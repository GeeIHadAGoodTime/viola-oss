# Security reporting

Report suspected vulnerabilities privately to `security@useviola.com`, or use the
repository's private vulnerability-reporting feature when available. Include the
affected source revision, reproduction steps, impact, and a minimal sanitized
example. Do not include live secrets or another person's data. Avoid public issue
reports that reveal an unpatched exploit.

The source core is designed for your own desktop or self-hosted environment.
Keep local APIs on loopback unless you have configured authentication and network
access deliberately. Never expose an unauthenticated development listener publicly.
Use your own provider/carrier credentials and protect your local state directory.

Verify the exact source and dependency inventory distributed with a release.
Update dependencies through tested changes; a successful installation does not
prove that the selected versions are free of advisories. Third-party components
and user-selected models have their own update and security policies.
