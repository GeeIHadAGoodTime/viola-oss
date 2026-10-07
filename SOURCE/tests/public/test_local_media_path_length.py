"""Local-path request validation; no player, device, network, or model calls.

Use the shipped request model and unchanged local-ID resolver. The HTTP test
has an inert handler: acceptance here proves request validation, not playback.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
import types
import unittest
from collections import UserDict
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("local_media_path_models", ROOT / "ui/api/models.py")
models = importlib.util.module_from_spec(spec)
spec.loader.exec_module(models)
models.CommandIn.model_rebuild(_types_namespace=vars(models))


def _path_of_length(size: int) -> str:
    prefix, suffix = "C:\\Music\\", "\\Track.wav"
    # Avoid depending on the host OS or creating very long real filenames.
    return prefix + "x" * (size - len(prefix) - len(suffix)) + suffix


def _local_resolver():
    source = ROOT / "intent/tools/media_tools.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    names = {"_resolve_local_file_path", "_local_track_id_from_uri", "_looks_like_local_path"}
    nodes = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)]
    for node in tree.body:
        if (isinstance(node, ast.FunctionDef) and node.name in names) or (
            isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "_LOCAL_FILE_EXTENSIONS" for target in node.targets)
        ):
            nodes.append(node)
    namespace = {}
    # Execute only the selected functions from this checked-in source file.
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), str(source), "exec"), namespace)  # noqa: S102
    return namespace["_resolve_local_file_path"]


class LocalMediaPathLengthTests(unittest.TestCase):
    def test_local_paths_pass_without_truncation(self):
        for size in (83, 86, 200, 201, 260, 1024, 32_767):
            with self.subTest(size=size):
                value = _path_of_length(size)
                self.assertEqual(len(value), size)
                self.assertEqual(models.PlayIn(query=value, source="local").query, value)

    def test_local_paths_remain_bounded(self):
        with self.assertRaises(ValidationError):
            models.PlayIn(query=_path_of_length(32_768), source="local")

    def test_other_sources_keep_the_200_character_limit(self):
        for source in (None, "ytsearch1", "url", "spotify_cdp", "youtube_music"):
            with self.subTest(source=source):
                value = "https://example.test/" + "x" * 179
                self.assertEqual(len(value), 200)
                self.assertEqual(models.PlayIn(query=value, source=source).query, value)
                with self.assertRaises(ValidationError):
                    models.PlayIn(query=value + "x", source=source)
        with self.assertRaises(ValidationError):
            models.PlayIn(query="x" * 201)

    def test_nonlocal_whitespace_does_not_evade_original_length_limit(self):
        with self.assertRaises(ValidationError):
            models.PlayIn(query=" " * 200 + "x", source="ytsearch1")

    def test_python_coercion_and_mapping_inputs_keep_the_search_limit(self):
        for source in (None, "ytsearch1", "spotify_cdp", "youtube_music"):
            for convert in (dict, UserDict, MappingProxyType):
                for query in ("x" * 201, b"x" * 201, bytearray(b"x" * 201)):
                    with (
                        self.subTest(source=source, convert=convert, query_type=type(query)),
                        self.assertRaises(ValidationError),
                    ):
                        models.PlayIn.model_validate(convert({"query": query, "source": source}))
                # As before, length counts decoded characters, not UTF-8 bytes.
                payload = convert({"query": ("é" * 200).encode(), "source": source})
                self.assertEqual(models.PlayIn.model_validate(payload).query, "é" * 200)

    def test_attribute_inputs_keep_source_specific_length_limits(self):
        for source in (None, "ytsearch1", "spotify_cdp", "youtube_music"):
            with self.subTest(source=source), self.assertRaises(ValidationError):
                models.PlayIn.model_validate(SimpleNamespace(query="x" * 201, source=source), from_attributes=True)
        path = _path_of_length(260)
        model = models.PlayIn.model_validate(SimpleNamespace(query=path, source="local"), from_attributes=True)
        self.assertEqual(model.query, path)

    def test_source_room_type_and_command_validation_are_retained(self):
        for source in ("LOCAL", "local_files", "local ", "unknown"):
            with self.subTest(source=source), self.assertRaises(ValidationError):
                models.PlayIn(query="Track.wav", source=source)
        for value in (None, 123, {}, [], ""):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                models.PlayIn(query=value, source="local")
        with self.assertRaises(ValidationError):
            models.PlayIn(query="Track.wav", source="local", target_room="x" * 81)
        with self.assertRaises(ValidationError):
            models.CommandIn(text="x" * 201)

    def test_path_spelling_and_existing_trim_are_preserved(self):
        for path in (r"C:\Music\A song.wav", r"\\server\share\Track.wav", "/home/music/été.wav"):
            with self.subTest(path=path):
                self.assertEqual(models.PlayIn(query="  " + path + "  ", source="local").query, path)

    def test_short_library_id_resolves_to_long_stored_path(self):
        path = _path_of_length(260)
        repo = SimpleNamespace(initialize=Mock(), get_track_by_id=Mock(return_value={"file_path": path}))
        db = types.ModuleType("music.providers.local.db")
        db.get_local_library_repo = lambda: repo
        resolve = _local_resolver()
        with patch.dict(sys.modules, {"music.providers.local.db": db}):
            resolved = resolve("17")
        repo.get_track_by_id.assert_called_once_with(17)
        self.assertEqual(resolved, path)
        self.assertEqual(models.PlayIn(query=resolved, source="local").query, path)

    def test_missing_library_row_still_fails_and_valid_retry_resolves(self):
        path = _path_of_length(260)
        repo = SimpleNamespace(
            initialize=Mock(), get_track_by_id=Mock(side_effect=[None, {"file_path": path}])
        )
        db = types.ModuleType("music.providers.local.db")
        db.get_local_library_repo = lambda: repo
        resolve = _local_resolver()
        with patch.dict(sys.modules, {"music.providers.local.db": db}):
            with self.assertRaisesRegex(ValueError, "not found"):
                resolve("17")
            self.assertEqual(models.PlayIn(query=resolve("17"), source="local").query, path)

    def test_http_rejection_stays_before_handler_and_valid_retry_is_accepted(self):
        app = FastAPI()
        received = []

        # Inert request-schema fixture, deliberately not Viola's live player.
        def accept(body):
            received.append((body.query, body.source))
            return {"accepted": True}

        accept.__annotations__["body"] = models.PlayIn
        app.post("/schema-test")(accept)
        with TestClient(app) as client:
            for payload in (
                {"query": _path_of_length(32_768), "source": "local"},
                {"query": "x" * 201, "source": "ytsearch1"},
                {"query": "Track.wav", "source": "local_files"},
            ):
                self.assertEqual(client.post("/schema-test", json=payload).status_code, 422)
            self.assertEqual(received, [])
            path = _path_of_length(260)
            self.assertEqual(client.post("/schema-test", json={"query": path, "source": "local"}).status_code, 200)
            self.assertEqual(received, [(path, "local")])


if __name__ == "__main__":
    unittest.main()
