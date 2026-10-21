# Billing

A simple billing / invoicing web app that runs on your own computer.
It is not tied to any one shop: on first launch each owner enters their
own store name, address, phone, GSTIN and account, and can change all of it
later under **Settings**.

## Run (Windows)

```powershell
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Then open http://127.0.0.1:5000

## First use

1. The setup page asks for your business details and owner account.
2. Add products, then create bills from **New Bill**.
3. Open **Settings** any time to change business details, invoice
   options, password, payment methods, GST, printing and more.

## Your data

Everything is stored in `data/billing.db`. Use **Settings -> Backup &
Restore -> Download backup** regularly and keep the file somewhere safe
(for example a pen drive or cloud folder).

The login session key is generated on first run and stored in
`data/secret.key`. Do not share that file. You can also set the
`BILLING_SECRET_KEY` environment variable instead.

> `main.py` is an old desktop prototype and is not used by the web app.
