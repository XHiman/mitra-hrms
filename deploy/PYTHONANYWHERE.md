# Hosting the MITRA HRMS demo on PythonAnywhere (free)

Result: a link such as `https://yourname.pythonanywhere.com` that anyone can open from any network.
Time: about 15 minutes. **Use demo / dummy data only.** The free tier is for trying the system out,
not for real staff records.

## 1. Create the account

1. Go to <https://www.pythonanywhere.com> → **Pricing & signup** → **Create a Beginner account** (free).
2. Your username becomes part of the link (`username.pythonanywhere.com`), so pick something like
   `mitrahrms`.

## 2. Upload the code

1. Open the **Files** tab. You are in `/home/<username>/`.
2. Click **Upload a file** and choose `deploy/mitra-hrms.zip` from this project.
3. Open the **Consoles** tab → **Bash**, and run:

```bash
unzip -o mitra-hrms.zip -d mitra-hrms
cd mitra-hrms
pip3.12 install --user "Flask>=3.0,<4"
export HRMS_SQLITE_JOURNAL=DELETE
flask --app run init-db
flask --app run seed-demo
```

You should see `Demo organisation loaded…`.

## 3. Create the web app

1. Open the **Web** tab → **Add a new web app** → **Next**.
2. Choose **Manual configuration** (not "Flask"), then **Python 3.12**, then **Next**.
3. On the web app page:
   - **Code → WSGI configuration file:** click the link. Delete everything in it, paste the contents
     of `deploy/pythonanywhere_wsgi.py`, replace `YOUR_USERNAME` with your username, and click **Save**.
   - **Static files:** add URL `/static/` with directory `/home/<username>/mitra-hrms/hrms/static`.
   - **Security:** turn **Force HTTPS** on.
4. Click the green **Reload** button at the top.

## 4. Open and prepare

1. Visit `https://<username>.pythonanywhere.com`.
2. Log in as `admin` / `Mitra@Pilot2026` → **Leave configuration**. Enter entitlements for Casual,
   Sick and Earned leave (demo values are fine). Employees cannot apply until this is done.
3. Share the link and the demo logins (`ceo`, `jtceo`, `js`, `hr`, `rahul`, `priya`, `amit`,
   `kavita`, `admin`; all use `Mitra@Pilot2026`).

## Good to know

- **The link is public.** Anyone who has it, plus the demo password, can log in. Don't enter real
  personal data.
- **Keep it alive:** free web apps pause after 3 months. On the Web tab, click **"Run until 3 months
  from today"** to extend.
- **If something fails:** the Web tab has an **Error log** link; the last lines explain the problem.
- **Updating after changes:** upload the new zip, run `unzip -o mitra-hrms.zip -d mitra-hrms`, then
  `cd mitra-hrms && HRMS_SQLITE_JOURNAL=DELETE flask --app run init-db`, then click **Reload**.
  Existing data is kept.
- **Starting over with fresh demo data:** delete `mitra-hrms/instance/hrms.db`, run `init-db` and
  `seed-demo` again, then click **Reload**.
