/**
 * Viola Cloud auth front-door components.
 *
 * CloudAuthGate is the entry point: mount it on the cloud surface and it
 * decides login / sign-up / reset / verify vs. the dashboard based on
 * useAuth().status. The individual screens are exported for tests and reuse.
 */
export { default as CloudAuthGate, CLOUD_AUTH_VIEW } from './CloudAuthGate';
export { default as LoginScreen } from './LoginScreen';
export { default as SignUpScreen } from './SignUpScreen';
export { default as ResetPasswordScreen } from './ResetPasswordScreen';
export { default as RecoveryConfirmScreen } from './RecoveryConfirmScreen';
export { default as VerifyEmailScreen } from './VerifyEmailScreen';
export { default as AuthLoadingScreen } from './AuthLoadingScreen';
export { isCloudSurface, isSpokeRoute } from './cloudSurface';
