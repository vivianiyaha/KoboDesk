# KoboDesk — deploy guide
Repo layout:
  index.html        <- the whole frontend (landing page + app, CSS and JS inline)
  netlify.toml
  render.yaml
  backend/main.py, backend/requirements.txt, backend/.env.example

## Backend on Render
1. Push the repo to GitHub -> Render -> New -> Blueprint (reads render.yaml).
2. Set FRONTEND_URL and ALLOWED_ORIGINS to your Netlify URL (no trailing slash) and PAYSTACK_SECRET_KEY.
3. Paystack dashboard -> Settings -> API & Webhooks -> webhook URL: https://<render-url>/api/billing/webhook
4. If your Render URL is not kobodesk-pjsr.onrender.com, change API_BASE in index.html and the CSP in netlify.toml.

## Frontend on Netlify
New site from Git. No build command is needed; netlify.toml publishes the root.
Before launch, set SUPPORT_WA in index.html to your WhatsApp number (digits only, e.g. 2348012345678).
The landing page is at / and the app is at /#app.
