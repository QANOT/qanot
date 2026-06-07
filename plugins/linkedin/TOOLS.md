# LinkedIn (Company Page)

Publish to a LinkedIn **company page** as the organization. Requires the
Community Management API product on the LinkedIn app and the authorizing user to
be a Super/Content Admin of the page.

## Setup (one time)

1. Create a LinkedIn app whose **only** product is **Community Management API**
   (it cannot coexist with other products), linked to the company page.
2. Add redirect URL `http://localhost:8080/callback` in the app's Auth settings.
3. Get a token:
   ```bash
   LINKEDIN_CLIENT_ID=xxx LINKEDIN_CLIENT_SECRET=yyy python plugins/linkedin/auth.py
   # headless server: add --manual and paste the redirected URL
   ```
   This writes `token.json` (the plugin reads `<workspace>/linkedin/token.json`,
   or an `access_token` in config).
4. Find the numeric org id with `linkedin_list_orgs`, set it as `org_id` in the
   plugin config.

Config (config.json `plugins` entry):
```json
{ "name": "linkedin", "config": { "org_id": "12345678" } }
```
Tokens last ~60 days — re-run `auth.py` to refresh `token.json` (no redeploy).

## Tools

| Tool | What it does |
|------|--------------|
| `linkedin_post` | Publish a post to the page. `text` (required) + optional `image_path` (local file, e.g. from `generate_image`). Posts publicly as the organization. |
| `linkedin_status` | Check token validity and which org is configured. |
| `linkedin_list_orgs` | List company pages you administer + their numeric org ids (needs `rw_organization_admin`). |

## Notes

- Author is `urn:li:organization:{org_id}` — posts appear as the **page**, not you.
- Images are uploaded via `registerUpload` → asset, then attached to the post.
- Only posts to the configured page; no third-party member data is accessed.
