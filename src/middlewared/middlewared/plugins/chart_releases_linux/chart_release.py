import collections
import copy
import errno
import itertools
import os
import shutil
import tempfile
import yaml

from pkg_resources import parse_version

from middlewared.schema import accepts, Dict, Str
from middlewared.service import CallError, CRUDService, filterable, job, private
from middlewared.utils import filter_list, get
from middlewared.validators import Match

from .utils import CHART_NAMESPACE_PREFIX, get_namespace, get_storage_class_name, Resources, run


class ChartReleaseService(CRUDService):

    class Config:
        namespace = 'chart.release'
        cli_namespace = 'app.chart_release'

    @filterable
    async def query(self, filters, options):
        """
        Query available chart releases.

        `options.extra.retrieve_resources` is a boolean when set will retrieve existing kubernetes resources
        in the chart namespace.

        `options.extra.history` is a boolean when set will retrieve all chart version upgrades for a chart release.

        `options.extra.include_chart_schema` is a boolean when set will retrieve the schema being used by
        the chart release in question.
        """
        if not await self.middleware.call('service.started', 'kubernetes'):
            # We use filter_list here to ensure that `options` are respected, options like get: true
            return filter_list([], filters, options)

        update_catalog_config = {}
        catalogs = await self.middleware.call('catalog.query', [], {'extra': {'item_details': True}})
        container_images = {}
        for image in await self.middleware.call('container.image.query'):
            for tag in image['repo_tags']:
                if not container_images.get(tag):
                    container_images[tag] = image['update_available']

        for catalog in catalogs:
            update_catalog_config[catalog['label']] = {}
            for train in catalog['trains']:
                train_data = {}
                for catalog_item in catalog['trains'][train]:
                    train_data[catalog_item] = max(
                        [parse_version(v) for v in catalog['trains'][train][catalog_item]['versions']],
                        default=parse_version('0.0.0')
                    )

                update_catalog_config[catalog['label']][train] = train_data

        k8s_config = await self.middleware.call('kubernetes.config')
        k8s_node_ip = await self.middleware.call('kubernetes.node_ip')
        options = options or {}
        extra = copy.deepcopy(options.get('extra', {}))
        retrieve_schema = extra.get('include_chart_schema')
        get_resources = extra.get('retrieve_resources')
        get_history = extra.get('history')

        if filters and len(filters) == 1 and filters[0][:2] == ['id', '=']:
            extra['namespace_filter'] = ['metadata.namespace', '=', f'{CHART_NAMESPACE_PREFIX}{filters[0][-1]}']
            resources_filters = [extra['namespace_filter']]
        else:
            resources_filters = [['metadata.namespace', '^', CHART_NAMESPACE_PREFIX]]

        ports_used = collections.defaultdict(list)
        for node_port_svc in await self.middleware.call(
            'k8s.service.query', [['spec.type', '=', 'NodePort']] + resources_filters
        ):
            release_name = node_port_svc['metadata']['namespace'][len(CHART_NAMESPACE_PREFIX):]
            ports_used[release_name].extend([
                {'port': p['node_port'], 'protocol': p['protocol']} for p in node_port_svc['spec']['ports']
            ])

        storage_classes = collections.defaultdict(lambda: None)
        for storage_class in await self.middleware.call('k8s.storage_class.query'):
            storage_classes[storage_class['metadata']['name']] = storage_class

        resources = {r.value: collections.defaultdict(list) for r in Resources}
        workload_status = collections.defaultdict(lambda: {'desired': 0, 'available': 0})

        for resource in Resources:
            for r_data in await self.middleware.call(f'k8s.{resource.name.lower()}.query', resources_filters):
                release_name = r_data['metadata']['namespace'][len(CHART_NAMESPACE_PREFIX):]
                resources[resource.value][release_name].append(r_data)
                if resource in (Resources.DEPLOYMENT, Resources.STATEFULSET):
                    workload_status[release_name]['desired'] += (r_data['status']['replicas'] or 0)
                    workload_status[release_name]['available'] += (r_data['status']['ready_replicas'] or 0)

        release_secrets = await self.middleware.call('chart.release.releases_secrets', extra)
        releases = []
        for name, release in release_secrets.items():
            config = {}
            release_data = release['releases'].pop(0)
            cur_version = release_data['chart_metadata']['version']

            for rel_data in filter(
                lambda r: r['chart_metadata']['version'] == cur_version,
                itertools.chain(reversed(release['releases']), [release_data])
            ):
                config.update(rel_data['config'])

            pods_status = workload_status[name]
            pod_diff = pods_status['available'] - pods_status['desired']
            status = 'ACTIVE'
            if pod_diff == 0 and pods_status['desired'] == 0:
                status = 'STOPPED'
            elif pod_diff < 0:
                status = 'DEPLOYING'

            # We will retrieve all host ports being used
            for pod in filter(lambda p: p['status']['phase'] == 'Running', resources[Resources.POD.value][name]):
                for container in pod['spec']['containers']:
                    ports_used[name].extend([
                        {'port': p['host_port'], 'protocol': p['protocol']}
                        for p in (container['ports'] or []) if p['host_port']
                    ])

            release_data.update({
                'path': os.path.join('/mnt', k8s_config['dataset'], 'releases', name),
                'dataset': os.path.join(k8s_config['dataset'], 'releases', name),
                'config': config,
                'status': status,
                'used_ports': ports_used[name],
                'pod_status': pods_status,
            })

            release_resources = {
                'storage_class': storage_classes[get_storage_class_name(name)],
                'host_path_volumes': await self.host_path_volumes(resources[Resources.POD.value][name]),
                **{r.value: resources[r.value][name] for r in Resources},
            }
            release_resources = {
                **release_resources,
                'container_images': list(set(
                    c['image']
                    for workload_type in ('deployments', 'statefulsets')
                    for workload in release_resources[workload_type]
                    for c in workload['spec']['template']['spec']['containers']
                ))
            }
            if get_resources:
                release_data['resources'] = release_resources

            if get_history:
                release_data['history'] = release['history']

            current_version = parse_version(release_data['chart_metadata']['version'])
            latest_version = update_catalog_config.get(release_data['catalog'], {}).get(
                release_data['catalog_train'], {}
            ).get(release_data['chart_metadata']['name'], parse_version(release_data['chart_metadata']['version']))

            release_data['update_available'] = latest_version > current_version

            if retrieve_schema:
                chart_path = os.path.join(release_data['path'], 'charts', release_data['chart_metadata']['version'])
                if os.path.exists(chart_path):
                    release_data['chart_schema'] = await self.middleware.call(
                        'catalog.item_version_details', chart_path
                    )
                else:
                    release_data['chart_schema'] = None

            release_data['container_images_update_available'] = any(
                container_images.get(tag) for tag in release_resources['container_images']
            )
            release_data['chart_metadata']['latest_chart_version'] = str(latest_version)
            release_data['portals'] = await self.middleware.call(
                'chart.release.retrieve_portals_for_chart_release', release_data, k8s_node_ip
            )
            if 'icon' not in release_data['chart_metadata']:
                release_data['chart_metadata']['icon'] = None

            releases.append(release_data)

        return filter_list(releases, filters, options)

    @private
    def retrieve_portals_for_chart_release(self, release_data, node_ip=None):
        questions_yaml_path = os.path.join(
            release_data['path'], 'charts', release_data['chart_metadata']['version'], 'questions.yaml'
        )
        if not os.path.exists(questions_yaml_path):
            return {}

        with open(questions_yaml_path, 'r') as f:
            portals = yaml.safe_load(f.read()).get('portals') or {}

        if not portals:
            return portals

        if not node_ip:
            node_ip = self.middleware.call_sync('kubernetes.node_ip')

        cleaned_portals = {}
        for portal_type, schema in portals.items():
            t_portals = []
            path = schema.get('path') or '/'
            for protocol in schema['protocols']:
                for host in schema['host']:
                    if host == '$node_ip':
                        host = node_ip
                    elif host.startswith('$variable-'):
                        host = get(release_data['config'], host[len('$variable-'):])

                    if not host:
                        continue

                    for port in schema['ports']:
                        if str(port).startswith('$variable-'):
                            port = get(release_data['config'], port[len('$variable-'):])
                        if not port:
                            # We are not going to add it to list of urls if port comes up as empty
                            continue
                        t_portals.append(f'{protocol}://{host}:{port}{path}')
            cleaned_portals[portal_type] = t_portals

        return cleaned_portals

    @private
    async def host_path_volumes(self, pods):
        host_path_volumes = []
        for pod in pods:
            for volume in filter(lambda v: v.get('host_path'), pod['spec']['volumes']):
                host_path_volumes.append(copy.deepcopy(volume))
        return host_path_volumes

    @private
    async def normalise_and_validate_values(self, item_details, values, update, release_ds, release_data=None):
        dict_obj = await self.middleware.call(
            'chart.release.validate_values', item_details, values, update, release_data,
        )
        return await self.middleware.call(
            'chart.release.get_normalised_values', dict_obj, values, update, {
                'release': {
                    'name': release_ds.split('/')[-1],
                    'dataset': release_ds,
                    'path': os.path.join('/mnt', release_ds),
                },
                'actions': [],
            }
        )

    @private
    async def perform_actions(self, context):
        for action in context['actions']:
            await self.middleware.call(f'chart.release.{action["method"]}', *action['args'])

    @accepts(
        Dict(
            'chart_release_create',
            Dict('values', additional_attrs=True),
            Str('catalog', required=True),
            Str('item', required=True),
            Str('release_name', required=True, validators=[Match(r'[a-z0-9]([-a-z0-9]*[a-z0-9])?$')]),
            Str('train', default='charts'),
            Str('version', default='latest'),
        )
    )
    @job(lock=lambda args: f'chart_release_create_{args[0]["release_name"]}')
    async def do_create(self, job, data):
        """
        Create a chart release for a catalog item.

        `release_name` is the name which will be used to identify the created chart release.

        `catalog` is a valid catalog id where system will look for catalog `item` details.

        `train` is which train to look for under `catalog` i.e stable / testing etc.

        `version` specifies the catalog `item` version.

        `values` is configuration specified for the catalog item version in question which will be used to
        create the chart release.
        """
        await self.middleware.call('kubernetes.validate_k8s_setup')
        if await self.query([['id', '=', data['release_name']]]):
            raise CallError(f'Chart release with {data["release_name"]} already exists.', errno=errno.EEXIST)

        catalog = await self.middleware.call(
            'catalog.query', [['id', '=', data['catalog']]], {'extra': {'item_details': True}}
        )
        if not catalog:
            raise CallError(f'Unable to locate {data["catalog"]!r} catalog', errno=errno.ENOENT)
        else:
            catalog = catalog[0]
        if data['train'] not in catalog['trains']:
            raise CallError(f'Unable to locate "{data["train"]}" catalog train.', errno=errno.ENOENT)
        if data['item'] not in catalog['trains'][data['train']]:
            raise CallError(f'Unable to locate "{data["item"]}" catalog item.', errno=errno.ENOENT)

        version = data['version']
        if version == 'latest':
            version = await self.middleware.call(
                'chart.release.get_latest_version_from_item_versions',
                catalog['trains'][data['train']][data['item']]['versions']
            )

        if version not in catalog['trains'][data['train']][data['item']]['versions']:
            raise CallError(f'Unable to locate "{data["version"]}" catalog item version.', errno=errno.ENOENT)

        item_details = catalog['trains'][data['train']][data['item']]['versions'][version]
        await self.middleware.call('catalog.version_supported_error_check', item_details)

        k8s_config = await self.middleware.call('kubernetes.config')
        release_ds = os.path.join(k8s_config['dataset'], 'releases', data['release_name'])
        # The idea is to validate the values provided first and if it passes our validation test, we
        # can move forward with setting up the datasets and installing the catalog item
        new_values = data['values']
        new_values, context = await self.normalise_and_validate_values(item_details, new_values, False, release_ds)

        job.set_progress(25, 'Initial Validation completed')

        # Now that we have completed validation for the item in question wrt values provided,
        # we will now perform the following steps
        # 1) Create release datasets
        # 2) Copy chart version into release/charts dataset
        # 3) Install the helm chart
        # 4) Create storage class
        storage_class_name = get_storage_class_name(data['release_name'])
        try:
            job.set_progress(30, 'Creating chart release datasets')

            for dataset in await self.release_datasets(release_ds):
                if not await self.middleware.call('zfs.dataset.query', [['id', '=', dataset]]):
                    await self.middleware.call('zfs.dataset.create', {'name': dataset, 'type': 'FILESYSTEM'})
                    await self.middleware.call('zfs.dataset.mount', dataset)

            job.set_progress(45, 'Created chart release datasets')

            chart_path = os.path.join('/mnt', release_ds, 'charts', version)
            await self.middleware.run_in_thread(lambda: shutil.copytree(item_details['location'], chart_path))

            job.set_progress(55, 'Completed setting up chart release')
            # Before finally installing the release, we will perform any actions which might be required
            # for the release to function like creating/deleting ix-volumes
            await self.perform_actions(context)

            namespace_name = get_namespace(data['release_name'])

            job.set_progress(65, f'Creating {namespace_name} for chart release')
            namespace_body = {
                'metadata': {
                    'labels': {
                        'catalog': data['catalog'],
                        'catalog_train': data['train'],
                        'catalog_branch': catalog['branch'],
                    },
                    'name': namespace_name,
                }
            }
            if not await self.middleware.call('k8s.namespace.query', [['metadata.name', '=', namespace_name]]):
                await self.middleware.call('k8s.namespace.create', {'body': namespace_body})
            else:
                await self.middleware.call('k8s.namespace.update', namespace_name, {'body': namespace_body})

            job.set_progress(75, 'Installing Catalog Item')

            with tempfile.NamedTemporaryFile(mode='w+') as f:
                f.write(yaml.dump(new_values))
                f.flush()
                # We will install the chart now and force the installation in an ix based namespace
                # https://github.com/helm/helm/issues/5465#issuecomment-473942223
                cp = await run(
                    ['helm', 'install', data['release_name'], chart_path, '-n', namespace_name, '-f', f.name],
                    check=False,
                )
            if cp.returncode:
                raise CallError(f'Failed to install catalog item: {cp.stderr}')

            storage_class = await self.middleware.call('k8s.storage_class.retrieve_storage_class_manifest')
            storage_class['metadata']['name'] = storage_class_name
            storage_class['parameters']['poolname'] = os.path.join(release_ds, 'volumes')
            if await self.middleware.call('k8s.storage_class.query', [['metadata.name', '=', storage_class_name]]):
                # It should not exist already, but even if it does, that's not fatal
                await self.middleware.call('k8s.storage_class.update', storage_class_name, storage_class)
            else:
                await self.middleware.call('k8s.storage_class.create', storage_class)
        except Exception:
            # Do a rollback here
            # Let's uninstall the release as well if it did get installed ( it is possible this might have happened )
            if await self.middleware.call('chart.release.query', [['id', '=', data['release_name']]]):
                delete_job = await self.middleware.call('chart.release.delete', data['release_name'])
                await delete_job.wait()
                if delete_job.error:
                    self.logger.error('Failed to uninstall helm chart release: %s', delete_job.error)
            else:
                await self.post_remove_tasks(data['release_name'])

            raise
        else:
            await self.middleware.call('chart.release.refresh_events_state', data['release_name'])
            job.set_progress(100, 'Chart release created')
            return await self.get_instance(data['release_name'])

    @accepts(
        Str('chart_release'),
        Dict(
            'chart_release_update',
            Dict('values', additional_attrs=True),
        )
    )
    @job(lock=lambda args: f'chart_release_update_{args[0]}')
    async def do_update(self, job, chart_release, data):
        """
        Update an existing chart release.

        `values` is configuration specified for the catalog item version in question which will be used to
        create the chart release.
        """
        release = await self.get_instance(chart_release)
        chart_path = os.path.join(release['path'], 'charts', release['chart_metadata']['version'])
        if not os.path.exists(chart_path):
            raise CallError(
                f'Unable to locate {chart_path!r} chart version for updating {chart_release!r} chart release',
                errno=errno.ENOENT
            )

        version_details = await self.middleware.call('catalog.item_version_details', chart_path)
        config = release['config']
        config.update(data['values'])
        # We use update=False because we want defaults to be populated again if they are not present in the payload
        # Why this is not dangerous is because the defaults will be added only if they are not present/configured for
        # the chart release.
        config, context = await self.normalise_and_validate_values(
            version_details, config, False, release['dataset'], release,
        )

        job.set_progress(25, 'Initial Validation complete')

        await self.perform_actions(context)

        with tempfile.NamedTemporaryFile(mode='w+') as f:
            f.write(yaml.dump(config))
            f.flush()

            cp = await run(
                ['helm', 'upgrade', chart_release, chart_path, '-n', get_namespace(chart_release), '-f', f.name],
                check=False,
            )
            if cp.returncode:
                raise CallError(f'Failed to update chart release: {cp.stderr.decode()}')

        job.set_progress(90, 'Syncing secrets for chart release')
        await self.middleware.call('chart.release.sync_secrets_for_release', chart_release)

        job.set_progress(100, 'Update completed for chart release')
        return await self.get_instance(chart_release)

    @accepts(Str('release_name'))
    @job(lock=lambda args: f'chart_release_delete_{args[0]}')
    async def do_delete(self, job, release_name):
        """
        Delete existing chart release.

        This will delete the chart release from the kubernetes cluster and also remove any associated volumes / data.
        To clarify, host path volumes will not be deleted which live outside the chart release dataset.
        """
        # For delete we will uninstall the release first and then remove the associated datasets
        await self.middleware.call('kubernetes.validate_k8s_setup')
        await self.get_instance(release_name)

        cp = await run(['helm', 'uninstall', release_name, '-n', get_namespace(release_name)], check=False)
        if cp.returncode:
            raise CallError(f'Unable to uninstall "{release_name}" chart release: {cp.stderr}')

        job.set_progress(50, f'Uninstalled {release_name}')
        job.set_progress(75, f'Waiting for {release_name!r} pods to terminate')
        await self.middleware.call('chart.release.wait_for_pods_to_terminate', get_namespace(release_name))

        await self.post_remove_tasks(release_name, job)

        await self.middleware.call('chart.release.remove_chart_release_from_events_state', release_name)

        job.set_progress(100, f'{release_name!r} chart release deleted')
        return True

    @accepts(Str('release_name'))
    @job(lock=lambda args: f'chart_release_redeploy_{args[0]}')
    async def redeploy(self, job, release_name):
        """
        Redeploy will initiate a rollout of new pods according to upgrade strategy defined by the chart release
        workloads. A good example for redeploying is updating kubernetes pods with an updated container image.
        """
        update_job = await self.middleware.call('chart.release.update', release_name, {'values': {}})
        return await job.wrap(update_job)

    @private
    async def post_remove_tasks(self, release_name, job=None):
        await self.remove_storage_class_and_dataset(release_name, job)
        await self.middleware.call('k8s.namespace.delete', get_namespace(release_name))

    @private
    async def remove_storage_class_and_dataset(self, release_name, job=None):
        storage_class_name = get_storage_class_name(release_name)
        if await self.middleware.call('k8s.storage_class.query', [['metadata.name', '=', storage_class_name]]):
            if job:
                job.set_progress(85, f'Removing {release_name!r} storage class')
            try:
                await self.middleware.call('k8s.storage_class.delete', storage_class_name)
            except Exception as e:
                self.logger.error('Failed to remove %r storage class: %s', storage_class_name, e)

        k8s_config = await self.middleware.call('kubernetes.config')
        release_ds = os.path.join(k8s_config['dataset'], 'releases', release_name)
        if await self.middleware.call('zfs.dataset.query', [['id', '=', release_ds]]):
            if job:
                job.set_progress(95, f'Removing {release_ds!r} dataset')
            await self.middleware.call('zfs.dataset.delete', release_ds, {'recursive': True, 'force': True})

    @private
    async def release_datasets(self, release_dataset):
        return [release_dataset] + [
            os.path.join(release_dataset, k) for k in ('charts', 'volumes', 'volumes/ix_volumes')
        ]

    @private
    async def get_chart_namespace_prefix(self):
        return CHART_NAMESPACE_PREFIX
