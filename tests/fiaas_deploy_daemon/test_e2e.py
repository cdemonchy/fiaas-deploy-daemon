#!/usr/bin/env python
# -*- coding: utf-8

import contextlib
import os
import socket
import subprocess
import sys
import time
from copy import deepcopy


import pytest
import re
import requests
import yaml
from k8s import config
from k8s.client import NotFound, Client
from k8s.models.autoscaler import HorizontalPodAutoscaler
from k8s.models.common import ObjectMeta
from k8s.models.deployment import Deployment
from k8s.models.ingress import Ingress
from k8s.models.service import Service

from fiaas_deploy_daemon.crd.types import FiaasApplication, FiaasStatus, FiaasApplicationSpec
from fiaas_deploy_daemon.tpr.status import create_name
from fiaas_deploy_daemon.tpr.types import PaasbetaApplication, PaasbetaApplicationSpec, PaasbetaStatus
from fiaas_deploy_daemon.tools import merge_dicts
from minikube import MinikubeError

from utils import wait_until, tpr_available, crd_available, tpr_supported, crd_supported

IMAGE1 = u"finntech/application-name:123"
IMAGE2 = u"finntech/application-name:321"
DEPLOYMENT_ID1 = u"deployment_id_1"
DEPLOYMENT_ID2 = u"deployment_id_2"
PATIENCE = 30
TIMEOUT = 5


def _fixture_names(fixture_value):
    name, data = fixture_value
    return name


@pytest.fixture(scope="session", params=("ClusterIP", "NodePort"))
def service_type(request):
    return request.param


@pytest.mark.integration_test
class TestE2E(object):

    @pytest.fixture(scope="module")
    def kubernetes(self, minikube_installer, service_type, k8s_version):
        try:
            minikube = minikube_installer.new(profile=service_type, k8s_version=k8s_version)
            try:
                minikube.start()
                yield {
                    "server": minikube.server,
                    "client-cert": minikube.client_cert,
                    "client-key": minikube.client_key,
                    "api-cert": minikube.api_cert
                }
            finally:
                minikube.delete()
        except MinikubeError as e:
            msg = "Unable to run minikube: %s"
            pytest.fail(msg % str(e))

    @pytest.fixture(autouse=True)
    def k8s_client(self, kubernetes):
        Client.clear_session()
        config.api_server = kubernetes["server"]
        config.debug = True
        config.verify_ssl = False
        config.cert = (kubernetes["client-cert"], kubernetes["client-key"])

    @pytest.fixture(scope="module")
    def fdd(self, kubernetes, service_type, k8s_version):
        port = self._get_open_port()
        args = ["fiaas-deploy-daemon",
                "--port", str(port),
                "--api-server", kubernetes["server"],
                "--api-cert", kubernetes["api-cert"],
                "--client-cert", kubernetes["client-cert"],
                "--client-key", kubernetes["client-key"],
                "--service-type", service_type,
                "--ingress-suffix", "svc.test.example.com",
                "--environment", "test",
                "--datadog-container-image", "DATADOG_IMAGE",
                "--strongbox-init-container-image", "STRONGBOX_IMAGE",
                ]
        if tpr_supported(k8s_version):
            args.append("--enable-tpr-support")
        if crd_supported(k8s_version):
            args.append("--enable-crd-support")
        fdd = subprocess.Popen(args, stdout=sys.stderr, env=merge_dicts(os.environ, {"NAMESPACE": "default"}))

        def ready():
            resp = requests.get("http://localhost:{}/healthz".format(port), timeout=TIMEOUT)
            resp.raise_for_status()

        try:
            wait_until(ready, "web-interface healthy", RuntimeError, patience=PATIENCE)
            if tpr_supported(k8s_version):
                wait_until(tpr_available(kubernetes, timeout=TIMEOUT), "TPR available", RuntimeError, patience=PATIENCE)
            if crd_supported(k8s_version):
                wait_until(crd_available(kubernetes, timeout=TIMEOUT), "CRD available", RuntimeError, patience=PATIENCE)
            yield "http://localhost:{}/fiaas".format(port)
        finally:
            self._end_popen(fdd)

    @pytest.fixture(ids=_fixture_names, params=(
            ("data/v2minimal.yml", {
                Service: "e2e_expected/v2minimal-service.yml",
                Deployment: "e2e_expected/v2minimal-deployment.yml",
                Ingress: "e2e_expected/v2minimal-ingress.yml",
            }),
            ("v2/data/examples/host.yml", {
                Service: "e2e_expected/host-service.yml",
                Deployment: "e2e_expected/host-deployment.yml",
                Ingress: "e2e_expected/host-ingress.yml",
            }),
            ("v2/data/examples/exec_config.yml", {
                Service: "e2e_expected/exec-service.yml",
                Deployment: "e2e_expected/exec-deployment.yml",
                Ingress: "e2e_expected/exec-ingress.yml",
            }),
            ("v2/data/examples/tcp_ports.yml", {
                Service: "e2e_expected/tcp_ports-service.yml",
                Deployment: "e2e_expected/tcp_ports-deployment.yml",
            }),
            ("v2/data/examples/single_tcp_port.yml", {
                Service: "e2e_expected/single_tcp_port-service.yml",
                Deployment: "e2e_expected/single_tcp_port-deployment.yml",
            }),
            ("v2/data/examples/partial_override.yml", {
                Service: "e2e_expected/partial_override-service.yml",
                Deployment: "e2e_expected/partial_override-deployment.yml",
                Ingress: "e2e_expected/partial_override-ingress.yml",
                HorizontalPodAutoscaler: "e2e_expected/partial_override-hpa.yml",
            }),
            ("v3/data/examples/v3minimal.yml", {
                Service: "e2e_expected/v3minimal-service.yml",
                Deployment: "e2e_expected/v3minimal-deployment.yml",
                Ingress: "e2e_expected/v3minimal-ingress.yml",
                HorizontalPodAutoscaler: "e2e_expected/v3minimal-hpa.yml",
            }),
            ("v3/data/examples/full.yml", {
                Service: "e2e_expected/v3full-service.yml",
                Deployment: "e2e_expected/v3full-deployment.yml",
                Ingress: "e2e_expected/v3full-ingress.yml",
                HorizontalPodAutoscaler: "e2e_expected/v3full-hpa.yml",
            }),
            ("v3/data/examples/multiple_hosts_multiple_paths.yml", {
                Service: "e2e_expected/multiple_hosts_multiple_paths-service.yml",
                Deployment: "e2e_expected/multiple_hosts_multiple_paths-deployment.yml",
                Ingress: "e2e_expected/multiple_hosts_multiple_paths-ingress.yml",
                HorizontalPodAutoscaler: "e2e_expected/multiple_hosts_multiple_paths-hpa.yml",
            }),
    ))
    def third_party_resource(self, request, k8s_version):
        fiaas_path, expected = request.param
        _skip_if_tpr_not_supported(k8s_version)

        fiaas_yml = _read_yml(request.fspath.dirpath().join("specs").join(fiaas_path).strpath)
        expected = {kind: _read_yml(request.fspath.dirpath().join(path).strpath) for kind, path in expected.items()}

        name = self._sanitize(fiaas_path)
        metadata = ObjectMeta(name=name, namespace="default", labels={"fiaas/deployment_id": DEPLOYMENT_ID1})
        spec = PaasbetaApplicationSpec(application=name, image=IMAGE1, config=fiaas_yml)
        return name, PaasbetaApplication(metadata=metadata, spec=spec), expected

    @pytest.fixture(ids=_fixture_names, params=(
            ("data/v2minimal.yml", {
                Service: "e2e_expected/v2minimal-service.yml",
                Deployment: "e2e_expected/v2minimal-deployment.yml",
                Ingress: "e2e_expected/v2minimal-ingress.yml",
            }),
            ("v2/data/examples/host.yml", {
                Service: "e2e_expected/host-service.yml",
                Deployment: "e2e_expected/host-deployment.yml",
                Ingress: "e2e_expected/host-ingress.yml",
            }),
            ("v2/data/examples/exec_config.yml", {
                Service: "e2e_expected/exec-service.yml",
                Deployment: "e2e_expected/exec-deployment.yml",
                Ingress: "e2e_expected/exec-ingress.yml",
            }),
            ("v2/data/examples/tcp_ports.yml", {
                Service: "e2e_expected/tcp_ports-service.yml",
                Deployment: "e2e_expected/tcp_ports-deployment.yml",
            }),
            ("v2/data/examples/single_tcp_port.yml", {
                Service: "e2e_expected/single_tcp_port-service.yml",
                Deployment: "e2e_expected/single_tcp_port-deployment.yml",
            }),
            ("v2/data/examples/partial_override.yml", {
                Service: "e2e_expected/partial_override-service.yml",
                Deployment: "e2e_expected/partial_override-deployment.yml",
                Ingress: "e2e_expected/partial_override-ingress.yml",
                HorizontalPodAutoscaler: "e2e_expected/partial_override-hpa.yml",
            }),
            ("v3/data/examples/v3minimal.yml", {
                Service: "e2e_expected/v3minimal-service.yml",
                Deployment: "e2e_expected/v3minimal-deployment.yml",
                Ingress: "e2e_expected/v3minimal-ingress.yml",
                HorizontalPodAutoscaler: "e2e_expected/v3minimal-hpa.yml",
            }),
            ("v3/data/examples/full.yml", {
                Service: "e2e_expected/v3full-service.yml",
                Deployment: "e2e_expected/v3full-deployment.yml",
                Ingress: "e2e_expected/v3full-ingress.yml",
                HorizontalPodAutoscaler: "e2e_expected/v3full-hpa.yml",
            }),
            ("v3/data/examples/multiple_hosts_multiple_paths.yml", {
                Service: "e2e_expected/multiple_hosts_multiple_paths-service.yml",
                Deployment: "e2e_expected/multiple_hosts_multiple_paths-deployment.yml",
                Ingress: "e2e_expected/multiple_hosts_multiple_paths-ingress.yml",
                HorizontalPodAutoscaler: "e2e_expected/multiple_hosts_multiple_paths-hpa.yml",
            }),
    ))
    def custom_resource_definition(self, request, k8s_version):
        fiaas_path, expected = request.param

        _skip_if_crd_not_supported(k8s_version)
        fiaas_yml = _read_yml(request.fspath.dirpath().join("specs").join(fiaas_path).strpath)
        expected = {kind: _read_yml(request.fspath.dirpath().join(path).strpath) for kind, path in expected.items()}

        name = self._sanitize(fiaas_path)
        metadata = ObjectMeta(name=name, namespace="default", labels={"fiaas/deployment_id": DEPLOYMENT_ID1})
        spec = FiaasApplicationSpec(application=name, image=IMAGE1, config=fiaas_yml)
        return name, FiaasApplication(metadata=metadata, spec=spec), expected

    @staticmethod
    def _sanitize(param):
        """must match the regex [a-z]([-a-z0-9]*[a-z0-9])?"""
        return re.sub("[^-a-z0-9]", "-", param.replace(".yml", ""))

    @staticmethod
    def _end_popen(popen):
        popen.terminate()
        time.sleep(1)
        if popen.poll() is None:
            popen.kill()

    @staticmethod
    def _get_open_port():
        with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
            s.bind(("", 0))
            return s.getsockname()[1]

    @staticmethod
    def _select_kinds(expected):
        if len(expected.keys()) > 0:
            return expected.keys()
        else:
            return [Service, Deployment, Ingress]

    @pytest.mark.usefixtures("fdd")
    def test_third_party_resource_deploy(self, third_party_resource, service_type):
        name, paasbetaapplication, expected = third_party_resource

        # check that k8s objects for name doesn't already exist
        kinds = self._select_kinds(expected)
        for kind in kinds:
            with pytest.raises(NotFound):
                kind.get(name)

        # First deploy
        paasbetaapplication.save()

        # Check that deployment status is RUNNING
        def _assert_status():
            status = PaasbetaStatus.get(create_name(name, DEPLOYMENT_ID1))
            assert status.result == u"RUNNING"

        wait_until(_assert_status, patience=PATIENCE)

        # Check deploy success
        wait_until(_deploy_success(name, kinds, service_type, IMAGE1, expected, DEPLOYMENT_ID1), patience=PATIENCE)

        # Redeploy, new image
        paasbetaapplication.spec.image = IMAGE2
        paasbetaapplication.metadata.labels["fiaas/deployment_id"] = DEPLOYMENT_ID2
        paasbetaapplication.save()

        # Check success
        wait_until(_deploy_success(name, kinds, service_type, IMAGE2, expected, DEPLOYMENT_ID2), patience=PATIENCE)

        # Cleanup
        PaasbetaApplication.delete(name)

        def cleanup_complete():
            for kind in kinds:
                with pytest.raises(NotFound):
                    kind.get(name)

        wait_until(cleanup_complete, patience=PATIENCE)

    @pytest.mark.usefixtures("fdd")
    def test_custom_resource_definition_deploy(self, custom_resource_definition, service_type):
        name, fiaas_application, expected = custom_resource_definition

        # check that k8s objects for name doesn't already exist
        kinds = self._select_kinds(expected)
        for kind in kinds:
            with pytest.raises(NotFound):
                kind.get(name)

        # First deploy
        fiaas_application.save()

        # Check that deployment status is RUNNING
        def _assert_status():
            status = FiaasStatus.get(create_name(name, DEPLOYMENT_ID1))
            assert status.result == u"RUNNING"

        wait_until(_assert_status, patience=PATIENCE)

        # Check deploy success
        wait_until(_deploy_success(name, kinds, service_type, IMAGE1, expected, DEPLOYMENT_ID1), patience=PATIENCE)

        # Redeploy, new image
        fiaas_application.spec.image = IMAGE2
        fiaas_application.metadata.labels["fiaas/deployment_id"] = DEPLOYMENT_ID2
        fiaas_application.save()

        # Check success
        wait_until(_deploy_success(name, kinds, service_type, IMAGE2, expected, DEPLOYMENT_ID2), patience=PATIENCE)

        # Cleanup
        FiaasApplication.delete(name)

        def cleanup_complete():
            for kind in kinds:
                with pytest.raises(NotFound):
                    kind.get(name)

        wait_until(cleanup_complete, patience=PATIENCE)


def _deploy_success(name, kinds, service_type, image, expected, deployment_id):
    def action():
        for kind in kinds:
            assert kind.get(name)
        dep = Deployment.get(name)
        assert dep.spec.template.spec.containers[0].image == image
        svc = Service.get(name)
        assert svc.spec.type == service_type

        for kind, expected_dict in expected.items():
            actual = kind.get(name)
            _assert_k8s_resource_matches(actual, expected_dict, image, service_type, deployment_id)

    return action


def _skip_if_tpr_not_supported(k8s_version):
    if not tpr_supported(k8s_version):
        pytest.skip("TPR not supported in version %s of kubernetes, skipping this test" % k8s_version)


def _skip_if_crd_not_supported(k8s_version):
    if not crd_supported(k8s_version):
        pytest.skip("CRD not supported in version %s of kubernetes, skipping this test" % k8s_version)


def _read_yml(yml_path):
    with open(yml_path, 'r') as fobj:
        yml = yaml.safe_load(fobj)
    return yml


def _assert_k8s_resource_matches(resource, expected_dict, image, service_type, deployment_id):
    actual_dict = deepcopy(resource.as_dict())
    expected_dict = deepcopy(expected_dict)

    # set expected test parameters
    _set_labels(expected_dict, image, deployment_id)

    if expected_dict["kind"] == "Deployment":
        _set_image(expected_dict, image)
        _set_env(expected_dict, image)
        _set_labels(expected_dict["spec"]["template"], image, deployment_id)

    if expected_dict["kind"] == "Service":
        _set_service_type(expected_dict, service_type)

    # the k8s client library doesn't return apiVersion or kind, so ignore those fields
    del expected_dict['apiVersion']
    del expected_dict['kind']

    # delete auto-generated k8s fields that we can't control in test data and/or don't care about testing
    _ensure_key_missing(actual_dict, "metadata", "creationTimestamp")  # the time at which the resource was created
    # indicates how many times the resource has been modified
    _ensure_key_missing(actual_dict, "metadata", "generation")
    # resourceVersion is used to handle concurrent updates to the same resource
    _ensure_key_missing(actual_dict, "metadata", "resourceVersion")
    _ensure_key_missing(actual_dict, "metadata", "selfLink")   # a API link to the resource itself
    # a unique id randomly for the resource generated on the Kubernetes side
    _ensure_key_missing(actual_dict, "metadata", "uid")
    # an internal annotation used to track ReplicaSets tied to a particular version of a Deployment
    _ensure_key_missing(actual_dict, "metadata", "annotations", "deployment.kubernetes.io/revision")
    # status is managed by Kubernetes itself, and is not part of the configuration of the resource
    _ensure_key_missing(actual_dict, "status")
    # autoscaling.alpha.kubernetes.io/conditions is automatically set when converting from
    # autoscaling/v2beta.HorizontalPodAutoscaler to autoscaling/v1.HorizontalPodAutoscaler internally in Kubernetes
    if isinstance(resource, HorizontalPodAutoscaler):
        _ensure_key_missing(actual_dict, "metadata", "annotations", "autoscaling.alpha.kubernetes.io/conditions")
    # pod.alpha.kubernetes.io/init-containers
    # pod.beta.kubernetes.io/init-containers
    # pod.alpha.kubernetes.io/init-container-statuses
    # pod.beta.kubernetes.io/init-container-statuses
    # are automatically set when converting from core.Pod to v1.Pod internally in Kubernetes (in some versions)
    if isinstance(resource, Deployment):
        _ensure_key_missing(actual_dict, "spec", "template", "metadata", "annotations", "pod.alpha.kubernetes.io/init-containers")
        _ensure_key_missing(actual_dict, "spec", "template", "metadata", "annotations", "pod.beta.kubernetes.io/init-containers")
        _ensure_key_missing(actual_dict, "spec", "template", "metadata", "annotations", "pod.alpha.kubernetes.io/init-container-statuses")
        _ensure_key_missing(actual_dict, "spec", "template", "metadata", "annotations", "pod.beta.kubernetes.io/init-container-statuses")
    if isinstance(resource, Service):
        _ensure_key_missing(actual_dict, "spec", "clusterIP")  # an available ip is picked randomly
        for port in actual_dict["spec"]["ports"]:
            _ensure_key_missing(port, "nodePort")  # an available port is randomly picked from the nodePort range

    pytest.helpers.assert_dicts(actual_dict, expected_dict)


def _set_image(expected_dict, image):
    expected_dict["spec"]["template"]["spec"]["containers"][0]["image"] = image


def _set_env(expected_dict, image):
    def generate_updated_env():
        for item in expected_dict["spec"]["template"]["spec"]["containers"][0]["env"]:
            if item["name"] == "VERSION":
                item["value"] = image.split(":")[-1]
            if item["name"] == "IMAGE":
                item["value"] = image
            yield item
    expected_dict["spec"]["template"]["spec"]["containers"][0]["env"] = list(generate_updated_env())


def _set_labels(expected_dict, image, deployment_id):
    expected_dict["metadata"]["labels"]["fiaas/version"] = image.split(":")[-1]
    expected_dict["metadata"]["labels"]["fiaas/deployment_id"] = deployment_id


def _set_service_type(expected_dict, service_type):
    expected_dict["spec"]["type"] = service_type


def _ensure_key_missing(d, *keys):
    key = keys[0]
    try:
        if len(keys) > 1:
            _ensure_key_missing(d[key], *keys[1:])
        else:
            del d[key]
    except KeyError:
        pass  # key was already missing
