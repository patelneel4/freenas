# -*- coding=utf-8 -*-
import json
import logging
import os
import re
import struct

import sysctl

from middlewared.service import CallError, private, Service

logger = logging.getLogger(__name__)


class EnterpriseService(Service):

    DATA = None
    ERROR = "Data not retrieved yet"

    @private
    def setup_m_series_nvdimm(self):
        try:
            # This should be ran before pool import so we cache the value obtained on first middlewared startup
            cache_path = "/tmp/m-series-nvdimm"
            if os.path.exists(cache_path):
                with open(cache_path) as f:
                    self.DATA = json.load(f)
                    return

            result = []

            i = 0
            while True:
                try:
                    desc = sysctl.filter(f"dev.nvdimm.{i}.%desc")[0].value
                except IndexError:
                    break

                size = int(re.match("NVDIMM region ([0-9]+)GB", desc).group(1))

                page = 0
                offset = 10
                sysctl.filter(f"dev.nvdimm.{i}.arg3")[0].value = (offset << 8 | page)
                status, version = struct.unpack("iB", sysctl.filter(f"dev.nvdimm.{i}.func27")[0].value)

                if status != 0:
                    raise ValueError(f"Invalid func27 status: {status}")

                version = hex(version)[2:]
                version = f"{version[0]}.{version[1:]}"

                result.append({
                    "index": i,
                    "size": size,
                    "firmware_version": version,
                })

                i += 1

            with open(cache_path, "w") as f:
                json.dump(result, f)

            self.DATA = result
        except Exception as e:
            self.middleware.logger.error("Unhandled exception in enterprise.setup_m_series_nvdimm", exc_info=True)
            self.ERROR = str(e)

    @private
    async def m_series_nvdimm(self):
        if self.DATA is None:
            raise CallError(self.ERROR)

        return self.DATA


async def setup(middleware):
    if (await middleware.call("truenas.get_chassis_hardware")).startswith("TRUENAS-M"):
        await middleware.call("enterprise.setup_m_series_nvdimm")
