# Rotterdam apartment alerts

The current free setup uses **GitHub + a dedicated Gmail account**. It does not require Cloudflare, a domain name, a Google developer project, or a computer left running.

The software checks **At Home Vastgoed, Wonen Hartje and Woonzeker** directly. It does not read website email alerts. Funda is not connected.

**Status:** Gmail connection tested successfully and receipt confirmed by the owner. Initial listing history was saved successfully on 17 November 2026. Recurring alerts are enabled in the workflow defaults. Check Actions → Apartment alerts for ongoing results.

## What it looks for

- Rotterdam itself.
- Advertised monthly rent up to and including €1,800. Utilities and service charges may be extra.
- At least two explicit bedrooms; total rooms do not substitute for bedrooms.
- Known closed, rented or unavailable listings are excluded. Student/temporary restrictions appear as notes where identified and need checking with the agent.

The first successful live scan establishes a starting point. It does not email every existing property. Later new matches are emailed once per source listing ID. The same property listed on two websites may still generate two emails.

## Your account steps

### 1. Create the dedicated Gmail account

Use a normal free personal Gmail account. You can choose any available address. This account sends the emails; your friend's receiving address can be with any provider.

Turn on [Google 2-Step Verification](https://myaccount.google.com/security). Then create an [app password](https://myaccount.google.com/apppasswords) named `Rotterdam apartment alerts`. This is a separate 16-character password for the program. Google only offers app passwords on eligible accounts with 2-Step Verification enabled.

Keep both passwords private. The normal Gmail password is never needed by this program. The app password goes directly into GitHub's secret field later, not into chat or a source file. If you cannot see Google's app-password option, stop there and we will check the account setup.

### 2. Publish the program in a GitHub project

Project: **kace03/sla-rotterdam**.

GitHub's free hosted checking at this frequency uses a **public repository**. The program and saved public listing records would be visible to others. Email addresses and the app password stay in separate GitHub secrets. No private account information is included in the prepared files.

The public repository was created by its owner. Account sign-in and Gmail secret entry are completed privately by the account owner.

The repository's default branch should contain this folder's files, including `.github/workflows/monitor.yml`. Do not upload the older Cloudflare prototype.

### 3. Add three private GitHub secrets

In the new project, open **Settings → Secrets and variables → Actions → Secrets → New repository secret**. Add:

| Name | Value |
| --- | --- |
| `GMAIL_SENDER` | The dedicated Gmail address |
| `GMAIL_APP_PASSWORD` | Its Google app password |
| `ALERT_RECIPIENT` | The address that should receive alerts |

For the first email test, you can use an address you can check yourself as `ALERT_RECIPIENT`, then replace it with your friend's address after confirming delivery.

These values belong only in the secret fields. They are not committed to the public program or listing history.

### 4. Run the preview and email test

Open **Actions → Apartment alerts → Run workflow**.

1. Choose `preview`. This checks the websites without sending email or changing alert history. Open the completed run's summary and confirm which sources succeeded from GitHub's network.
2. Choose `test_email`. This sends one simple connection-test message to the configured recipient. Confirm it actually arrives, including checking spam.

If a source is blocked from GitHub, a local successful test does not make it ready online. We must fix or disable that source rather than claim it is being monitored.

### 5. Live alerts and pause controls

The hosted preview, test email and initial history setup have passed. Live alerts are enabled by default in the workflow. The following optional repository variables override the defaults under **Settings → Secrets and variables → Actions → Variables**:

| Name | Value |
| --- | --- |
| `SEND_ALERTS` | `true` |
| `MONITOR_ENABLED` | `true` |

The starting history contains 11 existing matches, recorded without sending property emails. The schedule requests later runs every five minutes. Neither computer needs to remain on. A newly connected source still establishes its own first-run baseline.

To pause scheduled checks, set `MONITOR_ENABLED` to `false`. To allow previews but prevent scheduled property emails, set `SEND_ALERTS` to `false`.

## Speed and free-service limits

This is a best-effort free monitor. GitHub can delay or drop scheduled runs, and public-repository schedules can be disabled after 60 days without repository activity. It is not a guarantee of five-minute detection or of beating other applicants.

At Home follows its listing pagination. Wonen Hartje reads its Rotterdam offer page. Woonzeker discovers URLs through its public property sitemap; that sitemap's publication delay has not been measured. Detail caching reduces repeated requests; newly discovered URLs are checked immediately, while cached Wonen Hartje and Woonzeker details refresh after 15 minutes.

Keep using manual checks for important opportunities until the hosted monitor has demonstrated reliable operation. None of the tests guarantee availability, suitability, or acceptance by an agent.

## How it remembers sent listings

The program saves only public listing data and delivery status in `.monitor/state.json` on a separate `monitor-state` branch. It uses GitHub's temporary workflow token for this; you do not create a personal access token.

The workflow prevents overlapping runs. Before emailing, it saves a `sending` claim. After Gmail accepts the message, it saves `sent`. If submission or a later state update fails ambiguously, the claim stays held for review rather than being blindly resent. Check Gmail Sent before deciding whether such a record should be retried.

State writes failing, missing history or an unexpected state format stop the run. Source failures do not reset first-run history or result in sending stale pending matches from that source. GitHub's run summaries report counts and held deliveries without printing the sender or recipient address.

## Validation completed

- 10 tests for initial baselines, matching, unavailable listings, persistent delivery claims, duplicate prevention, failed state writes, mail construction, GitHub state requests and agency data parsing.
- The integrated Wonen Hartje and Woonzeker adapters parsed the previous live captures successfully: 10 rental properties and 46 rental properties respectively, yielding 1 and 2 available-looking matches.
- Hosted live previews successfully read all three included source sites from GitHub.
- Gmail accepted the connection test and its receipt was confirmed. GitHub-hosted checks for all three sources and remote history writes passed. First initialization saved 11 baseline matches with no pending or uncertain deliveries.

For local verification after installing `requirements.txt`, run `python -m unittest discover`. Running `python monitor.py` defaults to preview mode and does not send mail.

## References

- [GitHub standard runners are free for public repositories](https://docs.github.com/en/actions/how-tos/write-workflows/choose-where-workflows-run/choose-the-runner-for-a-job)
- [GitHub schedule behavior and limits](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)
- [Google app-password setup and account requirements](https://support.google.com/accounts/answer/185833/sign-in-with-app-passwords)
- [Gmail SMTP connection details](https://developers.google.com/workspace/gmail/imap/imap-smtp)
