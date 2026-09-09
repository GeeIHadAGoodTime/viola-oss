"""
WASAPI Audio Integration Package
=================================

Provides Windows Audio Session API (WASAPI) integration for Viola,
including loopback capture for Acoustic Echo Cancellation (AEC).

Components:
- loopback.py: Low-level WASAPI loopback capture
- capture_module.py: High-level capture module wrapper
- aec_reference_adapter.py: AEC reference source implementation
"""

from .aec_reference_adapter import WasapiAECReferenceAdapter
from .capture_module import NativeWasapiCaptureModule
from .loopback import IS_WINDOWS, NativeWasapiLoopback

__all__ = [
    "IS_WINDOWS",
    "NativeWasapiCaptureModule",
    "NativeWasapiLoopback",
    "WasapiAECReferenceAdapter",
]
