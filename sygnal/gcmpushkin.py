# -*- coding: utf-8 -*-
# Copyright 2014 Leon Handreke
# Copyright 2017 New Vector Ltd
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
import json
import logging
import time
from io import BytesIO
from json import JSONDecodeError

from opentracing import logs, tags
from prometheus_client import Histogram, Counter
from twisted.internet.defer import DeferredSemaphore
from twisted.web.client import HTTPConnectionPool, Agent, FileBodyProducer, readBody
from twisted.web.http_headers import Headers

from sygnal.exceptions import (
    TemporaryNotificationDispatchException,
    NotificationDispatchException,
)
from sygnal.utils import twisted_sleep, NotificationLoggerAdapter
from .exceptions import PushkinSetupException
from .notifications import Pushkin

SEND_TIME_HISTOGRAM = Histogram(
    "sygnal_gcm_request_time", "Time taken to send HTTP request to GCM"
)

RESPONSE_STATUS_CODES_COUNTER = Counter(
    "sygnal_gcm_status_codes",
    "Number of HTTP response status codes received from GCM",
    labelnames=["pushkin", "code"],
)

logger = logging.getLogger(__name__)

GCM_URL = b"https://fcm.googleapis.com/fcm/send"
MAX_TRIES = 3
RETRY_DELAY_BASE = 10
MAX_BYTES_PER_FIELD = 1024

# The error codes that mean a registration ID will never
# succeed and we should reject it upstream.
# We include NotRegistered here too for good measure, even
# though gcm-client 'helpfully' extracts these into a separate
# list.
BAD_PUSHKEY_FAILURE_CODES = [
    "MissingRegistration",
    "InvalidRegistration",
    "NotRegistered",
    "InvalidPackageName",
    "MismatchSenderId",
]

# Failure codes that mean the message in question will never
# succeed, so don't retry, but the registration ID is fine
# so we should not reject it upstream.
BAD_MESSAGE_FAILURE_CODES = ["MessageTooBig", "InvalidDataKey", "InvalidTtl"]

DEFAULT_MAX_CONNECTIONS = 20


class GcmPushkin(Pushkin):
    """
    Pushkin that relays notifications to Google/Firebase Cloud Messaging.
    """

    UNDERSTOOD_CONFIG_FIELDS = {"type", "api_key"}

    def __init__(self, name, sygnal, config, canonical_reg_id_store):
        super(GcmPushkin, self).__init__(name, sygnal, config)

        nonunderstood = set(self.cfg.keys()).difference(self.UNDERSTOOD_CONFIG_FIELDS)
        if len(nonunderstood) > 0:
            logger.warning(
                "The following configuration fields are not understood: %s",
                nonunderstood,
            )

        self.http_pool = HTTPConnectionPool(reactor=sygnal.reactor)
        self.max_connections = self.get_config(
            "max_connections", DEFAULT_MAX_CONNECTIONS
        )
        self.connection_semaphore = DeferredSemaphore(self.max_connections)
        self.http_pool.maxPersistentPerHost = self.max_connections

        self.http_agent = Agent(reactor=sygnal.reactor, pool=self.http_pool)

        self.db = sygnal.database
        self.canonical_reg_id_store = canonical_reg_id_store

        self.api_key = self.get_config("api_key")
        if not self.api_key:
            raise PushkinSetupException("No API key set in config")

    @classmethod
    async def create(cls, name, sygnal, config):
        """
        Override this if your pushkin needs to call async code in order to
        be constructed. Otherwise, it defaults to just invoking the Python-standard
        __init__ constructor.

        Returns:
            an instance of this Pushkin
        """
        logger.debug("About to set up CanonicalRegId Store")
        canonical_reg_id_store = CanonicalRegIdStore()
        await canonical_reg_id_store.setup(sygnal.database)
        logger.debug("Finished setting up CanonicalRegId Store")

        return cls(name, sygnal, config, canonical_reg_id_store)

    async def _perform_http_request(self, body, headers):
        """
        Perform an HTTP request to the FCM server with the body and headers
        specified.
        Args:
            body (nested dict): Body. Will be JSON-encoded.
            headers (Headers): HTTP Headers.

        Returns:

        """
        body_producer = FileBodyProducer(BytesIO(json.dumps(body).encode()))

        # we use the semaphore to actually limit the number of concurrent
        # requests, since the HTTPConnectionPool will actually just lead to more
        # requests being created but not pooled – it does not perform limiting.
        await self.connection_semaphore.acquire()
        try:
            response = await self.http_agent.request(
                b"POST", GCM_URL, headers=Headers(headers), bodyProducer=body_producer
            )
            response_text = (await readBody(response)).decode()
        except Exception as exception:
            raise TemporaryNotificationDispatchException(
                "GCM request failure"
            ) from exception
        finally:
            self.connection_semaphore.release()
        return response, response_text

    async def _request_dispatch(self, n, log, body, headers, pushkeys, span):
        poke_start_time = time.time()

        failed = []

        with SEND_TIME_HISTOGRAM.time():
            response, response_text = await self._perform_http_request(body, headers)

        RESPONSE_STATUS_CODES_COUNTER.labels(
            pushkin=self.name, code=response.code
        ).inc()

        log.debug("GCM request took %f seconds", time.time() - poke_start_time)

        span.set_tag(tags.HTTP_STATUS_CODE, response.code)

        if 500 <= response.code < 600:
            log.debug("%d from server, waiting to try again", response.code)

            retry_after = None

            for header_value in response.headers.getRawHeader(
                b"retry-after", default=[]
            ):
                retry_after = int(header_value)
                span.log_kv({"event": "gcm_retry_after", "retry_after": retry_after})

            raise TemporaryNotificationDispatchException(
                "GCM server error, hopefully temporary.", custom_retry_delay=retry_after
            )
        elif response.code == 400:
            log.error(
                "%d from server, we have sent something invalid! Error: %r",
                response.code,
                response_text,
            )
            # permanent failure: give up
            raise NotificationDispatchException("Invalid request")
        elif response.code == 401:
            log.error(
                "401 from server! Our API key is invalid? Error: %r", response_text
            )
            # permanent failure: give up
            raise NotificationDispatchException("Not authorised to push")
        elif 200 <= response.code < 300:
            try:
                resp_object = json.loads(response_text)
            except JSONDecodeError:
                raise NotificationDispatchException("Invalid JSON response from GCM.")
            if "results" not in resp_object:
                log.error(
                    "%d from server but response contained no 'results' key: %r",
                    response.code,
                    response_text,
                )
            if len(resp_object["results"]) < len(pushkeys):
                log.error(
                    "Sent %d notifications but only got %d responses!",
                    len(n.devices),
                    len(resp_object["results"]),
                )
                span.log_kv(
                    {
                        logs.EVENT: "gcm_response_mismatch",
                        "num_devices": len(n.devices),
                        "num_results": len(resp_object["results"]),
                    }
                )

            # determine which pushkeys to retry or forget about
            new_pushkeys = []
            for i, result in enumerate(resp_object["results"]):
                span.set_tag("gcm_regid_updated", "registration_id" in result)
                if "registration_id" in result:
                    await self.canonical_reg_id_store.set_canonical_id(
                        pushkeys[i], result["registration_id"]
                    )
                if "error" in result:
                    log.warning(
                        "Error for pushkey %s: %s", pushkeys[i], result["error"]
                    )
                    span.set_tag("gcm_error", result["error"])
                    if result["error"] in BAD_PUSHKEY_FAILURE_CODES:
                        log.info(
                            "Reg ID %r has permanently failed with code %r: "
                            "rejecting upstream",
                            pushkeys[i],
                            result["error"],
                        )
                        failed.append(pushkeys[i])
                    elif result["error"] in BAD_MESSAGE_FAILURE_CODES:
                        log.info(
                            "Message for reg ID %r has permanently failed with code %r",
                            pushkeys[i],
                            result["error"],
                        )
                    else:
                        log.info(
                            "Reg ID %r has temporarily failed with code %r",
                            pushkeys[i],
                            result["error"],
                        )
                        new_pushkeys.append(pushkeys[i])
            return failed, new_pushkeys

    async def dispatch_notification(self, n, device, context):
        log = NotificationLoggerAdapter(logger, {"request_id": context.request_id})

        pushkeys = [
            device.pushkey for device in n.devices if device.app_id == self.name
        ]
        # Resolve canonical IDs for all pushkeys

        if pushkeys[0] != device.pushkey:
            # Only send notifications once, to all devices at once.
            return []

        # The pushkey is kind of secret because you can use it to send push
        # to someone.
        # span_tags = {"pushkeys": pushkeys}
        span_tags = {"gcm_num_devices": len(pushkeys)}

        with self.sygnal.tracer.start_span(
            "gcm_dispatch", tags=span_tags, child_of=context.opentracing_span
        ) as span_parent:
            reg_id_mappings = await self.canonical_reg_id_store.get_canonical_ids(
                pushkeys
            )

            reg_id_mappings = {
                reg_id: canonical_reg_id or reg_id
                for (reg_id, canonical_reg_id) in reg_id_mappings.items()
            }

            inverse_reg_id_mappings = {v: k for (k, v) in reg_id_mappings.items()}

            data = GcmPushkin._build_data(n)
            headers = {
                b"User-Agent": ["sygnal"],
                b"Content-Type": ["application/json"],
                b"Authorization": ["key=%s" % (self.api_key,)],
            }

            # count the number of remapped registration IDs in the request
            span_parent.set_tag(
                "gcm_num_remapped_reg_ids_used",
                [k != v for (k, v) in reg_id_mappings.items()].count(True),
            )

            # TODO: Implement collapse_key to queue only one message per room.
            failed = []

            body = {"data": data, "priority": "normal" if n.prio == "low" else "high"}

            for retry_number in range(0, MAX_TRIES):
                mapped_pushkeys = [reg_id_mappings[pk] for pk in pushkeys]

                if len(pushkeys) == 1:
                    body["to"] = mapped_pushkeys[0]
                else:
                    body["registration_ids"] = mapped_pushkeys

                log.info("Sending (attempt %i) => %r", retry_number, mapped_pushkeys)

                try:
                    span_tags = {"retry_num": retry_number}

                    with self.sygnal.tracer.start_span(
                        "gcm_dispatch_try", tags=span_tags, child_of=span_parent
                    ) as span:
                        new_failed, new_pushkeys = await self._request_dispatch(
                            n, log, body, headers, mapped_pushkeys, span
                        )
                    pushkeys = new_pushkeys
                    failed += [
                        inverse_reg_id_mappings[canonical_pk]
                        for canonical_pk in new_failed
                    ]
                    if len(pushkeys) == 0:
                        break
                except TemporaryNotificationDispatchException as exc:
                    retry_delay = RETRY_DELAY_BASE * (2 ** retry_number)
                    if exc.custom_retry_delay is not None:
                        retry_delay = exc.custom_retry_delay

                    log.warning(
                        "Temporary failure, will retry in %d seconds",
                        retry_delay,
                        exc_info=True,
                    )

                    span_parent.log_kv(
                        {"event": "temporary_fail", "retrying_in": retry_delay}
                    )

                    await twisted_sleep(
                        retry_delay, twisted_reactor=self.sygnal.reactor
                    )

            if len(pushkeys) > 0:
                log.info("Gave up retrying reg IDs: %r", pushkeys)
            # Count the number of failed devices.
            span_parent.set_tag("gcm_num_failed", len(failed))
            return failed

    @staticmethod
    def _build_data(n):
        """
        Build the payload data to be sent.
        Args:
            n: Notification to build the payload for.

        Returns:
            JSON-compatible dict
        """
        data = {}
        for attr in [
            "event_id",
            "type",
            "sender",
            "room_name",
            "room_alias",
            "membership",
            "sender_display_name",
            "content",
            "room_id",
        ]:
            if hasattr(n, attr):
                data[attr] = getattr(n, attr)
                # Truncate fields to a sensible maximum length. If the whole
                # body is too long, GCM will reject it.
                if data[attr] is not None and len(data[attr]) > MAX_BYTES_PER_FIELD:
                    data[attr] = data[attr][0:MAX_BYTES_PER_FIELD]

        data["prio"] = "high"
        if n.prio == "low":
            data["prio"] = "normal"

        if getattr(n, "counts", None):
            data["unread"] = n.counts.unread
            data["missed_calls"] = n.counts.missed_calls

        return data


class CanonicalRegIdStore(object):
    TABLE_CREATE_QUERY = """
        CREATE TABLE IF NOT EXISTS gcm_canonical_reg_id (
            reg_id TEXT PRIMARY KEY,
            canonical_reg_id TEXT NOT NULL
        );
        """

    def __init__(self):
        self.db = None

    async def setup(self, db):
        """
        Prepares, if necessary, the database for storing canonical registration IDs.

        Separate method from the constructor because we wait for an async request
        to complete, so it must be an `async def` method.

        Args:
            db (adbapi.ConnectionPool): database to prepare

        """
        self.db = db

        await self.db.runQuery(self.TABLE_CREATE_QUERY)

    async def set_canonical_id(self, reg_id, canonical_reg_id):
        """
        Associates a GCM registration ID with a canonical registration ID.
        Args:
            reg_id (str): a registration ID
            canonical_reg_id (str): the canonical registration ID for `reg_id`
        """
        await self.db.runQuery(
            "INSERT OR REPLACE INTO gcm_canonical_reg_id VALUES (?, ?);",
            (reg_id, canonical_reg_id),
        )

    async def get_canonical_ids(self, reg_ids):
        """
        Retrieves the canonical registration ID for multiple registration IDs.

        Args:
            reg_ids (iterable): registration IDs to retrieve canonical registration
                IDs for.

        Returns (dict):
            mapping of registration ID to either its canonical registration ID,
            or `None` if there is no entry.
        """
        return {reg_id: await self.get_canonical_id(reg_id) for reg_id in reg_ids}

    async def get_canonical_id(self, reg_id):
        """
        Retrieves the canonical registration ID for one registration ID.

        Args:
            reg_id (str): registration ID to retrieve the canonical registration
                ID for.

        Returns (dict):
            its canonical registration ID, or `None` if there is no entry.
        """
        rows = await self.db.runQuery(
            "SELECT canonical_reg_id FROM gcm_canonical_reg_id WHERE reg_id = ?",
            (reg_id,),
        )

        if rows:
            return rows[0][0]
