# Rotterdam rental monitor

This is our apartment watcher for Rotterdam. It checks **At Home Vastgoed, Wonen Hartje and Woonzeker** directly and emails new matching listings to the recipient saved in GitHub. It runs on GitHub's servers using a dedicated Gmail sender, so neither computer needs to stay on. It does not use the websites' email alerts. Funda is not connected.

## Controls

Use the links below while signed in to GitHub as the repository owner. On the page that opens, press **Run workflow**, leave the branch on **main**, then press the green **Run workflow** button. No passwords or extra accounts are needed for these controls.

| Control | What it does |
| --- | --- |
| [Send a test email](https://github.com/kace03/sla-rotterdam/actions/workflows/test-email.yml) | Sends one connection-test message to the configured recipient. Does not check websites. |
| [Email all current matches](https://github.com/kace03/sla-rotterdam/actions/workflows/send-all.yml) | Checks all three sources and sends **one email containing every current match**, including listings already sent before. Does not change normal alert history. |
| [Email one random match](https://github.com/kace03/sla-rotterdam/actions/workflows/send-random.yml) | Checks the sources and emails one randomly chosen current match, even if it was sent before. Does not change normal alert history. Sends nothing if no matches are found. |
| [START automatic monitoring](https://github.com/kace03/sla-rotterdam/actions/workflows/start.yml) | Resumes automatic checks and new-listing emails from the next scheduled run. Preserves history. |
| [STOP all checks and emails](https://github.com/kace03/sla-rotterdam/actions/workflows/stop.yml) | Cancels the active monitor run when GitHub starts processing this command, then saves a persistent stop switch. Website checks and emails, including manual email controls, stay stopped until START is used. |
| [Check for new listings now](https://github.com/kace03/sla-rotterdam/actions/workflows/email-check.yml) | Runs a normal new-listing check immediately, respecting history and the stop switch. |

These are GitHub's built-in controls, rather than a separate hosted dashboard. Only people with permission to run this repository's workflows can use them. Clicking a link alone sends nothing. Each press of Run workflow is a separate request; repeating an email command deliberately sends it again. Avoid rerunning a failed email command until checking the sender's Sent folder.

After submitting a command, open its newest run. Its summary shows what was checked and how many emails Gmail accepted. A queued command has not finished yet. A green check on a command while stopped means it respected the stop switch; its summary says it is stopped. The latest START/STOP run and the saved `enabled` value in [monitor state](https://github.com/kace03/sla-rotterdam/blob/monitor-state/.monitor/state.json) show the switch setting. Older state without that field means enabled.

STOP cannot recall an email Gmail already accepted. GitHub still wakes scheduled jobs to read the switch while stopped, but they do not visit property websites or send email. START does not email all old listings.

## What counts as a match

- The listing says **Rotterdam**, not a nearby town.
- Advertised monthly rent is **up to and including EUR 1,800**. Utilities and service charges may cost extra.
- It explicitly has **at least two bedrooms**. Two rooms alone is not enough.
- It is not marked rented, closed or unavailable in the data we can read.

Student-only and other restrictions are included as notes where detected. A match still needs checking with the agent for eligibility, actual costs and availability.

## Automatic emails

The schedule requests a check every five minutes. Each source's first successful scan records existing matches without emailing them. Later, a previously unseen matching listing is emailed once per source ID. The same home advertised on two websites can produce two alerts. Normal checks send at most five pending alerts per run.

Before sending, the program saves a delivery claim. After Gmail accepts the email it records the result. If sending fails ambiguously, that listing is held for review instead of automatically risking a duplicate. Failed source checks do not reset history or send old pending listings from that source.

The history and start/stop switch live in `.monitor/state.json` on the separate `monitor-state` branch. They contain public property details and delivery status. Gmail credentials and the recipient are kept separately in GitHub secrets and are not written to source or listing history.

## Where the data comes from

At Home's public property pages supply listings and pagination. Wonen Hartje's Rotterdam overview supplies links to property pages. Woonzeker uses its public property sitemap to discover property pages. The monitor reads listing details directly; it cannot discover a Woonzeker listing before that site's sitemap exposes it.

Normal checks reuse Wonen Hartje and Woonzeker details for up to 15 minutes to reduce repeated requests. Newly discovered links are checked immediately. The all-matches and random-match commands refresh details instead of using that cache. The all-matches email identifies failed sources if only part of a scan succeeds. If every source fails, no digest is sent; if successful sources have zero matches, the digest says so.

## Limits

GitHub can delay or drop scheduled runs and can disable public-repository schedules after 60 days without repository activity. Five minutes is a requested interval, not a guaranteed delivery time. Website changes, blocking, stale source data and Gmail delivery delays can also affect results. The run summaries report source failures; this is a free best-effort watcher, not a guarantee of beating other applicants.

The dedicated Gmail connection has been tested and receipt confirmed. All three sources have previously passed hosted checks. Current run results are in [Actions](https://github.com/kace03/sla-rotterdam/actions).

## Maintenance

The existing repository secrets are `GMAIL_SENDER`, `GMAIL_APP_PASSWORD` and `ALERT_RECIPIENT`. Change the receiving address through GitHub Settings → Secrets and variables → Actions. Never put an app password in a source file. The optional `SEND_ALERTS=false` repository variable still disables normal automatic property emails; manual email controls are governed by the start/stop switch.

The program is split between `monitor.py` (commands, Gmail and saved history), `sources.py` (collection and matching), `agencies.py` (agency page parsing) and the workflows under `.github/workflows`. Tests run with `python -m unittest discover` after installing `requirements.txt`. A local `python monitor.py` defaults to a read-only preview.
