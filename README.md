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

## How the scraping works

### 1. GitHub starts a temporary computer

GitHub stores the code and runs it through **GitHub Actions**. A scheduled check or a manual control starts a temporary Linux computer called a runner. The runner downloads this repository, installs Python and the required libraries, restores the property-detail cache, and starts `monitor.py`.

That Python program does the scraping. GitHub itself does not understand apartments or search the websites for us. The runner first reads the saved start/stop switch and history through GitHub's API. If the switch is off, it finishes without contacting the property websites or Gmail.

### 2. The program requests public pages over HTTPS

From the runner, the program makes **HTTPS GET requests** to the property websites. A GET request asks a server to return the contents of a particular URL, much like opening that URL in a browser. The websites see requests coming from GitHub's runner, not from your home computer.

The response usually contains **HTML**: the text, links, labels and embedded data that a browser uses to build the visible page. A sitemap response contains **XML**, a structured list of page addresses. The program reads those responses directly. It does not take screenshots, click through a visible browser, or execute the website's JavaScript.

The network library is `curl_cffi`, configured with a Chrome-style connection profile. This makes aspects of its connection resemble Chrome's; it does not run Chrome or guarantee access through website blocking. The current program reads public pages without signing in to the property websites. It does not solve CAPTCHAs.

Requests are made one at a time, with at least about one second between their start times, and each request has a 20-second timeout. This keeps the program from requesting every property page at once.

### 3. Each website has its own extraction rules

Websites organise their data differently, so each source has its own reader. **Parsing** means finding known fields or labels in the returned text and turning their values into something the program can use.

#### At Home Vastgoed: read the property data inside the page

The program requests [At Home's property overview](https://athomevastgoed.nl/woningaanbod). Inside that page's source is a structured property collection associated with `SET_PROPERTIES_COLLECTION`. It is JSON, a text format with named fields and values that the website also uses to display its listings.

The reader extracts each property's ID, address/title, city, advertised price, bedroom count, link and status. For example, the bedroom field is `no_bedrooms`, the price is `ah_price`, and the city comes from `location.name`. Only listings whose status is `new` or `active` are retained.

If the collection gives a `next_page_url`, the program requests that page too. It follows these links until there are no more, with a maximum of ten pages and checks for repeated or unexpected page addresses. For this source, the fields needed for matching come from the overview's embedded data; it does not need to visit every property's detail page.

#### Wonen Hartje: follow property links and read labelled details

The program requests [Wonen Hartje's Rotterdam overview](https://wonenhartje.nl/rotterdam/aanbod-rotterdam/) and uses **Beautiful Soup**, an HTML-reading library, to find property links matching that site's Rotterdam rental URL pattern.

For each discovered property, it reads the detail page or a recent cached copy of its parsed details. It finds the page elements containing the price, title and specification labels, including `Slaapkamers`, `Verhuurvorm` and `Status`. It takes the bedroom count from the bedroom specification, rather than treating the overview's room count as bedrooms.

A property must say it is for rent and have an available status. If its status field is empty but the page's availability label says `Beschikbaar`, that label is used. The current reader identifies Rotterdam from the property's Rotterdam URL pattern. It does **not** independently verify the address against a map or postcode database. A simple text check also adds a note when it detects a student-only restriction; it does not understand every possible eligibility condition.

#### Woonzeker: discover pages through a sitemap, then decode embedded data

The program requests [Woonzeker's public property sitemap](https://woonzeker.com/__sitemap__/properties-0.xml). A sitemap is a machine-readable list of URLs. The program keeps links under `/huur/woningen/` and reads their detail pages or recent cached details.

Those pages contain a script block named `__NUXT_DATA__`. This holds the structured data used by the site's Nuxt web framework. Some values refer to other positions in a shared list, so the decoder follows those references to reconstruct the property record.

It then reads the price from `handover.price`, the bedrooms from `characteristic.layout.number_of_bedrooms`, and the city from `address.location`. Both the property's stage and status code must say `available`. A property that has not appeared in the sitemap cannot yet be discovered by this reader.

### 4. Convert the results into one common listing format

The three readers produce the same basic fields, despite getting them from different places. This is an illustrative record, not an actual property:

```json
{
  "id": "athome:12345",
  "source": "At Home Vastgoed",
  "title": "Example street 10",
  "city": "Rotterdam",
  "rent": 1750,
  "bedrooms": 2,
  "url": "https://athomevastgoed.nl/example-property"
}
```

Prices and bedroom counts are converted to numbers where the reader needs to do so. The common filter then checks city, rent, bedrooms and availability. For example, the record above passes the price and bedroom rules; the same record with rent 1850 or only one bedroom fails. Missing or unreadable price/bedroom values do not qualify.

The program uses explicit fields and fixed rules. It does not use AI to guess missing bedrooms, estimate the rent from a photo, or infer that a two-room apartment has two bedrooms. Its conclusions depend on what each website publishes and on the extraction rules continuing to match that website.

### 5. Compare with history, then send through Gmail

For a normal live check, the program compares qualifying source IDs with the saved history. An ID such as `athome:12345` combines the source with its property identifier. Existing IDs are recognised even though each run uses a fresh temporary computer.

Each source's first successful live check creates a baseline. On later checks, newly discovered matching IDs become pending alerts. Before sending a pending alert, the program checks it against that run's successfully collected results. The source results can include detail records cached within the 15-minute window described below.

The runner connects separately to Gmail's encrypted SMTP service, authenticates using the app password stored in GitHub secrets, and submits the email to the configured recipient. The property websites are not involved in sending the email. The all-matches and random-match controls use current scan results without changing the normal listing history.

### 6. Cache details and recognise failed scans

Normal checks request the discovery pages again, but reuse parsed Wonen Hartje and Woonzeker detail records for up to **15 minutes**. GitHub Actions restores that cache onto the temporary runner and saves it after changes. New links have no cached record, so their detail pages are fetched immediately. The all-matches and random-match commands clear the loaded detail cache before collecting, so they request fresh detail pages.

Caching reduces repeated traffic, but it means a recently changed price or availability can remain out of date until its detail record is refreshed. Even a freshly fetched page can contain stale information supplied by the agent.

A blocked request, timeout, unexpected response or missing required page structure is recorded as a source failure. The HTML reader also checks for common access-challenge page titles. Discovery returning no links on Wonen Hartje or Woonzeker is treated cautiously as a failure rather than assumed to mean there are no properties. A failed source is not treated as a successful empty scan, and its old pending alerts are not sent during that run.

These checks catch many changes, but cannot detect every possible mistake or website redesign. If a site changes its field names, HTML layout, status values or sitemap format, its reader may need updating.

## Limits

GitHub can delay or drop scheduled runs and can disable public-repository schedules after 60 days without repository activity. Five minutes is a requested interval, not a guaranteed delivery time. Website changes, blocking, stale source data and Gmail delivery delays can also affect results. The run summaries report source failures; this is a free best-effort watcher, not a guarantee of beating other applicants.

The dedicated Gmail connection has been tested and receipt confirmed. All three sources have previously passed hosted checks. Current run results are in [Actions](https://github.com/kace03/sla-rotterdam/actions).

## Maintenance

The existing repository secrets are `GMAIL_SENDER`, `GMAIL_APP_PASSWORD` and `ALERT_RECIPIENT`. Change the receiving address through GitHub Settings → Secrets and variables → Actions. Never put an app password in a source file. The optional `SEND_ALERTS=false` repository variable still disables normal automatic property emails; manual email controls are governed by the start/stop switch.

The program is split between `monitor.py` (commands, Gmail and saved history), `sources.py` (collection and matching), `agencies.py` (agency page parsing) and the workflows under `.github/workflows`. Tests run with `python -m unittest discover` after installing `requirements.txt`. A local `python monitor.py` defaults to a read-only preview.
