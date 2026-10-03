# Source snapshot binding

Source revision: `2b07f2cb80cb7ceabc789670d7aad97afa438ecf`.

Initial native window size, minimum dimensions and position now respect Qt
available logical screen geometry, including taskbar space and negative monitor
origins. This addresses controls opening outside smaller displays.

Five existing/new unittest methods pass. Five screen cases fail before and pass
after. A real bundled-PySide6 offscreen geometry probe also confirms the original
1400x800 window exceeds its800x800 work area while the corrected window fits.
That probe is not a normal installed GUI acceptance run; that remains pending.

Frontend and dependency inputs are unchanged. Existing inventories and prior
qualification receipts are retained without claiming a fresh advisory scan,
new product release or deployment.
