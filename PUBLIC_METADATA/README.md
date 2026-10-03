# Source snapshot binding

Source revision: `bf964ed8dc21eaa558aa8dd31119516eb17c5422`.

The expanded calendar remains outside inert ancestors while its background stage
is hidden. Sibling dialogs still make the entire underlying card inert. Cancel
and Escape restore the saved theme and accent after a Customize preview; Save
preserves the selected theme.

Both defects were observed in the signed Windows 1.0.4 product. Regression probes
fail against the preceding source and pass with these changes. The combined
frontend passes 138 test files / 992 tests and its production build on Linux.
Installed-candidate verification and public delivery remain pending.

Dependency inputs are unchanged from f0a3f33074df76269dca76293aa413ed8e1bfb46.
The frontend inventory retains that revision's successful Windows installation
(job 111110266035); five Python inventories are unchanged. This is not a new
runtime or release qualification.
