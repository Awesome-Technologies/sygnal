# -*- coding: utf-8 -*-
# Copyright 2014 OpenMarket Ltd
# Copyright 2018, 2019 New Vector Ltd
# Copyright 2019 The Matrix.org Foundation C.I.C.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


import importlib
import logging
import os
import sys
from logging.handlers import WatchedFileHandler

import yaml
from twisted.internet import reactor
from twisted.internet.defer import gatherResults, ensureDeferred

from sygnal.http import PushGatewayApiServer
from .database import Database

logger = logging.getLogger(__name__)

CONFIG_SECTIONS = ["http", "log", "apps", "db", "metrics"]
CONFIG_DEFAULTS = {
    "port": "5000",
    "loglevel": "info",
    "logfile": "",
    "dbfile": "sygnal.db",
}


class Sygnal(object):
    def __init__(self, config, custom_reactor=reactor):
        self.config = config
        self.reactor = custom_reactor
        self.pushkins = {}

    def _setup(self):
        cfg = self.config

        logging.getLogger().setLevel(getattr(logging, cfg["log"]["loglevel"].upper()))
        logfile = cfg["log"]["logfile"]
        if logfile != "":
            handler = WatchedFileHandler(logfile)
            formatter = logging.Formatter(
                "%(asctime)s [%(process)d] %(levelname)-5s "
                "%%(request_id)s %(name)s %(message)s"
            )
            handler.setFormatter(formatter)
            logging.getLogger().addHandler(handler)
        else:
            logging.basicConfig()

        # TODO if cfg.has_option("metrics", "sentry_dsn"):
        #     # Only import sentry if enabled
        #     import sentry_sdk
        #     from sentry_sdk.integrations.flask import FlaskIntegration
        #     sentry_sdk.init(
        #         dsn=cfg.get("metrics", "sentry_dsn"),
        #         integrations=[FlaskIntegration()],
        #     )

        # TODO if cfg.has_option("metrics", "prometheus_port"):
        #     prometheus_client.start_http_server(
        #         port=cfg.getint("metrics", "prometheus_port"),
        #         addr=cfg.get("metrics", "prometheus_addr"),
        #     )

        self.database = Database(cfg["db"]["dbfile"], self.reactor)

        for app_id, app_cfg in cfg["apps"].items():
            try:
                self.pushkins[app_id] = self._make_pushkin(app_cfg, app_id)
            except Exception:
                logger.exception("Failed to load module for kind %s", app_cfg)
                raise

        if len(self.pushkins) == 0:
            logger.error("No app IDs are configured. Edit sygnal.yaml to define some.")
            sys.exit(1)

        logger.info("Configured with app IDs: %r", self.pushkins.keys())
        logger.info("Setup completed")

    def _make_pushkin(self, app_type, app_name, app_config):
        if "." in app_type:
            kind_split = app_type.rsplit(".", 1)
            to_import = kind_split[0]
            to_construct = kind_split[1]
        else:
            to_import = f"sygnal.{app_type}pushkin"
            to_construct = f"{app_type.capitalize()}Pushkin"

        logger.info("Importing pushkin module: %s", to_import)
        pushkin_module = importlib.import_module(to_import)
        logger.info("Creating pushkin: %s", to_construct)
        clarse = getattr(pushkin_module, to_construct)
        return clarse(app_name, self, app_config)

    def run(self):
        self._setup()
        port = int(self.config.get("http", "port"))
        pushgateway_api = PushGatewayApiServer(self)
        logger.info("Listening on port %d", port)

        start_deferred = gatherResults(
            [ensureDeferred(pushkin.start(self)) for pushkin in self.pushkins.values()],
            consumeErrors=True,
        )

        def on_started(_):
            logger.info("Starting listening")
            self.reactor.listenTCP(port, pushgateway_api.site)

        start_deferred.addCallback(on_started)

        logger.info("Starting pushkins")
        self.reactor.run()

    def shutdown(self):
        pass  # TODO


def parse_config():
    config_path = os.getenv("SYGNAL_CONF", "sygnal.yaml")
    with open(config_path) as file_handle:
        return yaml.safe_load(file_handle)


if __name__ == "__main__":
    config = parse_config()
    print(config)
    stop()  # todo
    sygnal = Sygnal(config)
    sygnal.run()
