# Security Policy

## Supported versions

Security fixes are provided for the latest minor release on the default branch.
Older releases must be upgraded before a report can be considered resolved.

## Reporting

Do not include real inquiry bodies, email addresses, IP addresses, credentials,
tokens, or production database extracts in a report. Use GitHub private
vulnerability reporting for this repository. If that channel is unavailable,
open a public issue containing only a request for a private contact channel.

Please include the affected version or commit, the required permissions, the
request or management-command path, expected behavior, observed behavior, and a
minimal reproduction using synthetic data.

## Security boundaries

- Package installation and migrations are trusted deployment operations.
- CMS administrators may configure deployed plugins but cannot install Python
  packages or execute arbitrary Python or HTML through this plugin.
- Public form submissions are untrusted and must pass CSRF, signed-token,
  honeypot, rate-limit, size, field-validation, and idempotency controls.
- Viewing inquiry payloads requires the dedicated
  `contact_forms.view_contact_content` permission.
- SMTP is executed only by the Outbox worker, never by the public HTTP request.
- Email delivery is at-least-once. SMTP does not provide an atomic
  send-and-acknowledge operation, so an operating-system failure after SMTP
  acceptance but before the database acknowledgement may cause a duplicate
  email. Duplicate HTTP submissions remain database-idempotent.
- The maintenance process and `check_contact_forms_health` are required
  production components, not optional examples.

Out of scope: malicious code installed by a trusted deployment operator,
compromise of the CMS host or database administrator, and availability failures
of external SMTP or DNS providers. Reports showing a bypass from an untrusted
actor into those boundaries remain in scope.
