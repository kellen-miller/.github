# Shared repository defaults

`renovate-config.json` makes Renovate onboarding suggest the shared preset in
[`kellen-miller/ci`](https://github.com/kellen-miller/ci).

Renovate discovers this file in the public `.github` repository when no
`kellen-miller/renovate-config` default preset exists. Existing repositories keep
using their committed configuration until it is updated explicitly.

The Renovate app must have access to each repository and interactive onboarding
enabled. This file selects the onboarding preset; it does not change app access
or override silent mode. Fork processing follows the app's installation settings.
