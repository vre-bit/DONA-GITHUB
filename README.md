# Credit Note Desk (Alcove Realty)

A Flask web app that reads your Gmail for credit-note emails, has an OpenAI model classify them, and shows them by vendor.

## What you need
- Python 3.10 or newer
- A Google OAuth client (Client ID and Client Secret, free, about 5 minutes)
- An OpenAI API key (platform.openai.com)

## One-time setup
1. Install packages: `pip install -r requirements.txt`
2. Go to console.cloud.google.com and create a project.
3. APIs and Services > Library > enable **Gmail API**.
4. APIs and Services > OAuth consent screen > User type **External**. Add your own email under **Test users**.
5. Credentials > Create credentials > **OAuth client ID** > Application type **Web application**.
   Under *Authorized redirect URIs* add exactly: `http://localhost:8080/oauth2callback`
6. Copy `config.example.json` to `config.json` and fill in `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`,
   `OPENAI_API_KEY` and a random `SECRET_KEY`. (Environment variables of the same names also work.)

## Run
    python server.py

Open **http://localhost:8080** (use `localhost`, not `127.0.0.1`, so it matches the redirect URI).
Press **Connect Gmail and sync**. You are sent to Google, allow read-only Gmail access, and are brought back; the first sync starts automatically.

## How it works
- Sign-in uses the server-side OAuth code flow. Google tokens stay on the Flask server and never reach the browser; the access token is refreshed automatically.
- The page reads Gmail threads through `/api/gmail/...`, which only allows thread reads.
- Each batch of threads is sent to `/api/classify`, which calls OpenAI with the official Python SDK. The OpenAI key stays on the server.
- Results are cached in your browser, so later syncs only classify new or changed threads.
- **ERP pending bills:** the server reads pending bills from the ERP (`ERP_API_URL`) and the page lists them by vendor, by default only those assigned to `ERP_USER` (`dona.basu`). Each bill shows what it is waiting for (credit note, MTC, warranty certificate, invoice correction, GST, rate/PO, read from the ERP remark), its age, and whether a matching email exists (same vendor and invoice number in the classified emails).
- **Vendor follow-up mail:** **Draft follow-up mail** on a vendor writes a mail listing that vendor's pending bills and fills in the vendor's address from past Gmail threads. Nothing is sent until you review it and press **Approve & send**; it then goes from your own Gmail and the bills are marked "Followed up". Sending needs the Gmail "send email" permission, so sign out and sign in again once after updating.
- New mail alerts that mention a pending ERP invoice (or come from a vendor with pending bills) are tagged with the ERP bill.
- **New mail alerts:** while the page is open and signed in, it checks Gmail every 2 minutes for new mail about credit notes, MTCs (material test certificates), warranties and invoices. Each one shows a desktop popup (press **Turn on desktop popups** once), a list at the top of the page, and the count in the tab title. Unhandled alerts are repeated as a reminder every 30 minutes until you press **Done**.
- Google sign-ins are kept in `tokens.db`, so they survive server restarts. **Sign out** removes and revokes yours.

## Notes
- Email text is sent to the OpenAI API for classification.
- Google will show an "unverified app" warning because it is your own test app. Choose Advanced > Continue.
- `OPENAI_MODEL` defaults to `gpt-4.1`; set it to another model your key can use if you get a "model not found" error.
- The app only reads mail. It cannot send, delete or change anything.
- Amounts appear only where the emails state them.

## Hosting under a sub-path (e.g. `/p/dona-project`)
- Run with gunicorn: `gunicorn server:app`. A `SCRIPT_NAME` (or `URL_PREFIX`) env var, or an `X-Forwarded-Prefix` header from the proxy, sets the prefix; it works whether or not the proxy strips it.
- In Google Cloud Console, add the public redirect URI too, e.g. `https://<your-host>/p/dona-project/oauth2callback`.
- Set `SECRET_KEY` on the host so sign-ins survive restarts. Google sign-ins are stored in `tokens.db` next to `server.py`.
