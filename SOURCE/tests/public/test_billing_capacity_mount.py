"""Public integration seam preserves private guards and standalone behavior."""

from __future__ import annotations

import ast
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]


def mounting_function():
    source = ROOT / "backend/fastapi_app.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name == "_ensure_billing_capacity_routes")
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), function], type_ignores=[])
    namespace = {}
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
                self.assertEqual(client.get("/v1/billing/capacity/catalog").json(), {"available": False})
                self.assertEqual(client.post("/v1/billing/capacity/change", json={}).status_code, 403)
            self.assertEqual(sum(route.path == "/v1/billing/capacity/change" for route in app.routes), 1)

    def test_standalone_without_company_extension_remains_usable(self):
        self.boundary.company_service_module_available = lambda *args, **kwargs: False
        with patch.dict(sys.modules, {self.boundary.__name__: self.boundary}):
            app = FastAPI()
            self.mount(app)
            self.assertFalse(any(route.path.startswith("/v1/billing") for route in app.routes))

    def test_broken_installed_company_extension_is_not_silently_omitted(self):
        def missing(*args, **kwargs):
            raise RuntimeError("Installed company module missing")

        self.boundary.company_service_module_available = missing
        with patch.dict(sys.modules, {self.boundary.__name__: self.boundary}):
            with self.assertRaisesRegex(RuntimeError, "Installed company module missing"):
                self.mount(FastAPI())


if __name__ == "__main__":
    unittest.main()
