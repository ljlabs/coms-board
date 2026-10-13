# Security policy

Swarm Agent is a local-first, single-user application. The dashboard has no
authentication, and workspace validation does not sandbox agent processes.
Do not expose the server through a public interface or reverse proxy. See the
[README security section](README.md#security) for the supported trust model.
ACP backend processes inherit the server environment and run with the server
user's permissions. A backend's native tools can read environment values and
files available to that account, including outside the selected workspace.
Use a low-privilege account and keep unrelated secrets out of the server
environment.

## Reporting a vulnerability

Please do not publish exploit details in a public issue. Use GitHub's private
vulnerability reporting for this repository if it is enabled. If it is not
available, contact the maintainers privately through the repository owner's
GitHub profile. Include the affected commit or release, the impact, and a
minimal reproduction. Do not include live credentials, database files, or
private board content in a report.
