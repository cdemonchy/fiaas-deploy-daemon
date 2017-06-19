#!/usr/bin/env python
# -*- coding: utf-8
from __future__ import absolute_import

import posixpath
from blinker import signal


class Reporter(object):
    """Report results of deployments to pipeline"""
    def __init__(self, config, session):
        self._environment = config.environment
        self._infrastructure = config.infrastructure
        self._session = session
        self._callback_urls = {}
        signal("deploy_started").connect(self._handle_started)
        signal("deploy_success").connect(self._handle_success)
        signal("deploy_failed").connect(self._handle_failure)

    def register(self, deployment_id, url):
        self._callback_urls[deployment_id] = url

    def _handle_started(self, sender, app_spec):
        self._handle_signal(u"deploy_started", app_spec)

    def _handle_success(self, sender, app_spec):
        self._handle_signal(u"deploy_end", app_spec)

    def _handle_failure(self, sender, app_spec):
        self._handle_signal(u"deploy_end", app_spec, status=u"failure")

    def _handle_signal(self, event_name, app_spec, status=u"success"):
        base_url = self._callback_urls.get(app_spec.deployment_id)
        if base_url:
            task_name = u"fiaas_{}-{}_{}".format(self._environment, self._infrastructure, event_name)
            url = posixpath.join(base_url, task_name, status)
            self._session.post(url, json={u"description": u"From fiaas-deploy-daemon"})
