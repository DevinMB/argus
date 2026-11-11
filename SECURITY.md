# Security

Argus can change your hosts: it patches them and deploys to them. A hole in it matters.

## Report a vulnerability

Report it privately. On this repository, open **Security**, then **Report a vulnerability**. Do not open a public issue for it.

Say what you found, which version of Argus and of the companion it affects, and how to reproduce it. You get an answer within a week. A fix ships as a patch release, and the release notes credit you unless you would prefer otherwise.

## Supported versions

The newest 1.x release gets security fixes. The image tag `:1` follows it, and companions update themselves from your Argus.

## What Argus holds and how it is protected

[docs/security.md](docs/security.md) covers what Argus stores, what a companion can and cannot do on a host, and which outside services Argus calls.
