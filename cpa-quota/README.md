# AI Proxy Quotas — Noctalia 5

Native bar widget and popup for the CPA Quota API Extension. No browser is required for quota monitoring.

- Bar: available / total accounts, plus an error indicator when disconnected.
- Popup: account availability, remaining quota bars, UTC reset dates, snapshot timestamp.
- Left click opens the popup; right click refreshes upstream quotas.
- Refresh button forces one scan (30-second guard). Regular 60-second polling respects the proxy's 30-minute cache.
- API failures preserve the last successful snapshot and visibly mark it stale.
- Account emails are hidden by default. Enable them in plugin settings if wanted.
- The optional Usage Report button opens your existing web dashboard.

## Requirements

Noctalia 5 with plugin API 24+, and the CPA Quota API Extension on an HTTPS proxy.

## Setup

Set the proxy URL and the path of a file holding the management key in the plugin settings. Keep the key file mode 0600; it is sent through Noctalia's native HTTPS client, never on a command line. Then add `AI Proxy Quotas` (`buildpulsar/cpa-quota:quota`) in Noctalia Settings → Bar.

To open without a bar button:

```sh
noctalia msg panel-toggle buildpulsar/cpa-quota:panel
```

## Validation

`noctalia plugins lint /path/to/cpa-quota`

The plugin was loaded and its popup visually checked in Noctalia 5.2.1 against the live API. Service tests cover pagination, failed authentication, malformed responses, failed dispatch, and retaining stale data.

To disable:

```sh
noctalia msg plugins disable buildpulsar/cpa-quota
```

MIT license.
