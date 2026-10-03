# Source snapshot binding

Source revision: `f389fb7d98e0166382f0ceb1029334715d105e64`.

Retain the original minimatch compatibility patch as a reference so the existing
immutable private/public source ownership mapping remains valid. npm postinstall
continues to invoke only the bounded native script. patch-package and its
vulnerable transitive graph remain absent. No executable code or dependency
resolution changes here.

The paired private mapping change adds only the two newly published script/test
paths. The original Windows composition failure is preserved as the negative
control; neither the mapping verifier nor immutable-ownership guard is weakened.
The preceding chat/calendar/appearance source passed all Windows checks, but
installed-candidate and public-delivery qualification remain pending.
