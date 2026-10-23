"""Public integration seam preserves private guards and standalone behavior."""

from __future__ import annotations

import ast
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Response
from fastapi.testclient import TestClient

from contracts.fastapi import attach_response_contract

ROOT = Path(__file__).resolve().parents[2]


def mounting_function(name="_ensure_billing_capacity_routes"):
    source = ROOT / "backend/fastapi_app.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name == name)
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), function], type_ignores=[])
    namespace = {"APIRouter": APIRouter}
    exec(compile(ast.fix_missing_locations(module), str(source), "exec"), namespace)
    return namespace[function.name]


class BillingCapacityMount(unittest.TestCase):
    def setUp(self):
        self.boundary = types.ModuleType("services.company_service_boundary")
        self.routes = types.ModuleType("billing.capacity_routes")
        self.mount = mounting_function()

    def test_preserves_private_router_dependencies_and_mounts_once(self):
        def denied():
            raise HTTPException(403, "Consent required")

        router = APIRouter()

        @router.get("/capacity/catalog")
        def catalog():
            return {"available": False}

        @router.post("/capacity/change", dependencies=[Depends(denied)])
        def change():
            self.fail("The billing guard must run before a change")

        self.routes.capacity_router = router
        self.boundary.company_service_module_available = lambda *args, **kwargs: True
        with patch.dict(sys.modules, {self.boundary.__name__: self.boundary, self.routes.__name__: self.routes}):
            app = FastAPI()
            self.mount(app)
            self.mount(app)
            with TestClient(app) as client:
                assert client.get("/v1/billing/capacity/catalog").json() == {"available": False}
                assert client.post("/v1/billing/capacity/change", json={}).status_code == 403
            assert sum(route.path == "/v1/billing/capacity/change" for route in app.routes) == 1

    def test_standalone_without_company_extension_remains_usable(self):
        self.boundary.company_service_module_available = lambda *args, **kwargs: False
        with patch.dict(sys.modules, {self.boundary.__name__: self.boundary}):
            app = FastAPI()
            self.mount(app)
            assert not any(route.path.startswith("/v1/billing") for route in app.routes)

    def test_broken_installed_company_extension_is_not_silently_omitted(self):
        def missing(*args, **kwargs):
            raise RuntimeError("Installed company module missing")

        self.boundary.company_service_module_available = missing
        caught = None
        with patch.dict(sys.modules, {self.boundary.__name__: self.boundary}):
            try:
                self.mount(FastAPI())
            except RuntimeError as failure:
                caught = failure
        assert caught is not None
        assert "Installed company module missing" in str(caught)

    def test_desktop_usage_routes_preserve_authentication_and_mount_once(self):
        routes = types.ModuleType("billing.routes")

        def authenticated():
            raise HTTPException(401, "Sign-in required")

        def usage(user=Depends(authenticated)):
            self.fail("Unauthenticated usage read must not reach cloud forwarding")

        routes.get_subscription_status = usage
        routes.get_usage = usage
        routes.get_usage_today = usage
        routes.SpendUsageTodayResponse = dict
        with patch.dict(sys.modules, {routes.__name__: routes}):
            app = FastAPI()
            mount = mounting_function("_ensure_billing_usage_routes")
            mount(app)
            mount(app)
            with TestClient(app) as client:
                assert client.get("/billing/status").status_code == 401
                assert client.get("/billing/usage").status_code == 401
                assert client.get("/billing/usage/today").status_code == 401
            assert sum(route.path == "/billing/usage" for route in app.routes) == 1
            assert sum(route.path == "/billing/status" for route in app.routes) == 1


def cloud_body(payload, status_code=200):
    """What the desktop cloud proxy returns: the cloud's own JSON, verbatim."""
    return Response(content=json.dumps(payload), status_code=status_code, media_type="application/json")


class ForwardedCloudBillingReadsUnderEnvelopeContract(unittest.TestCase):
    """The desktop app runs attach_response_contract; forwarded cloud reads must survive it."""

    STATUS = {"user_id": "user-a", "plan_id": "pro_monthly", "status": "active", "has_paid_access": True}
    USAGE = {"monthly_percent": 12, "monthly_capped": True, "weekly_percent": 3, "weekly_capped": True}
    CAPACITY = {"monthly_price_cents": 2000, "selectable": True}

    def build_app(self):
        routes = types.ModuleType("billing.routes")
        routes.get_subscription_status = lambda: cloud_body(self.STATUS)
        routes.get_usage = lambda: cloud_body(self.USAGE)
        routes.get_usage_today = lambda: cloud_body({"plan_id": "pro_monthly", "monthly_spent_cents": 5})
        routes.SpendUsageTodayResponse = None

        capacity = APIRouter()
        capacity.add_api_route("/capacity", lambda: cloud_body(self.CAPACITY), methods=["GET"])
        capacity.add_api_route("/capacity/catalog", lambda: cloud_body({"levels": [2000]}), methods=["GET"])
        capacity.add_api_route(
            "/capacity/changes/{operation_id}", lambda operation_id: cloud_body({"status": "applied"}), methods=["GET"]
        )
        capacity_module = types.ModuleType("billing.capacity_routes")
        capacity_module.capacity_router = capacity
        boundary = types.ModuleType("services.company_service_boundary")
        boundary.company_service_module_available = lambda *args, **kwargs: True

        app = FastAPI()
        # A bare-body route that is NOT a cloud billing forward keeps the check.
        app.add_api_route("/v1/billing/capacityx", lambda: {"plain": True}, methods=["GET"])
        attach_response_contract(app)
        with patch.dict(sys.modules, {routes.__name__: routes, capacity_module.__name__: capacity_module,
                                      boundary.__name__: boundary}):
            mounting_function("_ensure_billing_usage_routes")(app)
            mounting_function("_ensure_billing_capacity_routes")(app)
        return app

    def test_plan_usage_and_capacity_reads_reach_the_ui_verbatim(self):
        with TestClient(self.build_app()) as client:
            for path, expected in (
                ("/billing/status", self.STATUS),
                ("/billing/usage", self.USAGE),
                ("/billing/usage/today", {"plan_id": "pro_monthly", "monthly_spent_cents": 5}),
                ("/v1/billing/capacity", self.CAPACITY),
                ("/v1/billing/capacity/catalog", {"levels": [2000]}),
                ("/v1/billing/capacity/changes/op123", {"status": "applied"}),
            ):
                with self.subTest(path=path):
                    response = client.get(path)
                    assert response.status_code == 200, (path, response.text)
                    assert response.json() == expected, path

    def test_exemption_does_not_widen_to_other_paths(self):
        with TestClient(self.build_app()) as client:
            response = client.get("/v1/billing/capacityx")
        assert response.status_code == 500
        assert response.json()["error"]["code"] == "response_contract_violation"


if __name__ == "__main__":
    unittest.main()
