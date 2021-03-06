import asyncio
import errno
import os
import shutil

import middlewared.sqlalchemy as sa

from middlewared.schema import Bool, Dict, Str, ValidationErrors
from middlewared.service import accepts, CallError, CRUDService, private

from .utils import convert_repository_to_path

OFFICIAL_LABEL = 'OFFICIAL'
TMP_IX_APPS_DIR = '/tmp/ix-applications'


class CatalogModel(sa.Model):
    __tablename__ = 'services_catalog'

    label = sa.Column(sa.String(255), nullable=False, unique=True, primary_key=True)
    repository = sa.Column(sa.Text(), nullable=False)
    branch = sa.Column(sa.String(255), nullable=False)
    builtin = sa.Column(sa.Boolean(), nullable=False, default=False)


class CatalogService(CRUDService):

    class Config:
        datastore = 'services.catalog'
        datastore_extend = 'catalog.catalog_extend'
        datastore_extend_context = 'catalog.catalog_extend_context'
        cli_namespace = 'app.catalog'

    @private
    async def catalog_extend_context(self, extra):
        k8s_dataset = (await self.middleware.call('kubernetes.config'))['dataset']
        catalogs_dir = os.path.join('/mnt', k8s_dataset, 'catalogs') if k8s_dataset else f'{TMP_IX_APPS_DIR}/catalogs'
        return {
            'catalogs_dir': catalogs_dir,
            'extra': extra or {},
        }

    @private
    async def catalog_extend(self, catalog, context):
        catalog.update({
            'location': os.path.join(
                context['catalogs_dir'], convert_repository_to_path(catalog['repository'], catalog['branch'])
            ),
            'id': catalog['label'],
        })
        extra = context['extra']
        if extra.get('item_details'):
            catalog['trains'] = await self.middleware.call(
                'catalog.items', catalog['label'], {'cache': extra.get('cache', True)},
            )
            catalog['healthy'] = all(
                app['healthy'] for train in catalog['trains'] for app in catalog['trains'][train].values()
            )
        return catalog

    @accepts(
        Dict(
            'catalog_create',
            Bool('force', default=False),
            Str('label', required=True, empty=False),
            Str('repository', required=True, empty=False),
            Str('branch', default='master'),
            register=True,
        )
    )
    async def do_create(self, data):
        verrors = ValidationErrors()
        # We normalize the label
        data['label'] = data['label'].upper()

        if await self.query([['id', '=', data['label']]]):
            verrors.add('catalog_create.label', 'A catalog with specified label already exists', errno=errno.EEXIST)

        if await self.query([['repository', '=', data['repository']], ['branch', '=', data['branch']]]):
            for k in ('repository', 'branch'):
                verrors.add(
                    f'catalog_create.{k}', 'A catalog with same repository/branch already exists', errno=errno.EEXIST
                )

        verrors.check()

        if not data.pop('force'):
            # We will validate the catalog now to ensure it's valid wrt contents / format
            path = os.path.join(
                TMP_IX_APPS_DIR, 'validate_catalogs', convert_repository_to_path(data['repository'], data['branch'])
            )
            try:
                await self.middleware.call('catalog.update_git_repository', {**data, 'location': path}, True)
                await self.middleware.call('catalog.validate_catalog_from_path', path)
            finally:
                await self.middleware.run_in_thread(shutil.rmtree, path, ignore_errors=True)

        await self.middleware.call('datastore.insert', self._config.datastore, data)

        asyncio.ensure_future(self.middleware.call('catalog.sync', data['label']))

        return await self.get_instance(data['label'])

    @accepts(
        Str('id'),
    )
    def do_delete(self, id):
        catalog = self.middleware.call_sync('catalog.get_instance', id)
        if catalog['builtin']:
            raise CallError('Builtin catalogs cannot be deleted')

        ret = self.middleware.call_sync('datastore.delete', self._config.datastore, id)

        if os.path.exists(catalog['location']):
            shutil.rmtree(catalog['location'], ignore_errors=True)

        # Let's delete any unhealthy alert if we had one
        self.middleware.call_sync('alert.oneshot_delete', 'CatalogNotHealthy', id)

        return ret

    @private
    async def official_catalog_label(self):
        return OFFICIAL_LABEL
