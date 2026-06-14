# 🚀 Buscador de trabajos remotos React Native / Mobile

Un bot que corre solo en **GitHub Actions** (gratis), busca trabajos remotos de
React Native / Mobile en varias APIs públicas, filtra los nuevos desde la última
corrida, y te los manda por **email**.

Fuentes: RemoteOK · Remotive · Arbeitnow · Himalayas · We Work Remotely

---

## 📦 Qué hace

1. Cada 6 horas (configurable) pega a 4 APIs de trabajos remotos.
2. Filtra solo los que matchean React Native / Mobile / Expo / iOS / Android.
3. Compara contra `seen_jobs.json` para mandarte **solo los nuevos**.
4. Te manda un email bonito con los trabajos y sus links.
5. Guarda los vistos para no repetirte.

---

## ⚙️ Cómo montarlo (paso a paso)

### 1. Crea un repo en GitHub
- Ve a github.com → New repository → ponle `job-bot` (puede ser privado).
- Sube estos archivos (find_jobs.py, seen_jobs.json, y la carpeta .github/).

  Desde tu terminal:
  ```bash
  cd job-bot
  git init
  git add .
  git commit -m "Initial commit"
  git branch -M main
  git remote add origin https://github.com/felipetrick12/job-bot.git
  git push -u origin main
  ```

### 2. Consigue una contraseña de aplicación para el email

**Si usas Gmail:**
1. Activa la verificación en 2 pasos en tu cuenta Google.
2. Ve a https://myaccount.google.com/apppasswords
3. Genera una "App password" (16 caracteres). Esa es tu `SMTP_PASS`.
   (NO uses tu contraseña normal de Gmail, no funciona.)

   - SMTP_HOST = `smtp.gmail.com`
   - SMTP_PORT = `587`
   - SMTP_USER = `tucorreo@gmail.com`
   - SMTP_PASS = la app password de 16 caracteres
   - EMAIL_TO  = a dónde quieres que lleguen (puede ser el mismo)

**Si usas Outlook/Hotmail (tu duvanli@hotmail.es):**
   - SMTP_HOST = `smtp-mail.outlook.com`
   - SMTP_PORT = `587`
   - SMTP_USER = `duvanli@hotmail.es`
   - SMTP_PASS = tu contraseña (o app password si tienes 2FA)
   - EMAIL_TO  = duvanli@hotmail.es

   > Nota: Outlook a veces bloquea SMTP en cuentas nuevas. Si falla, Gmail es lo más confiable.

### 3. Guarda los secrets en GitHub
En tu repo: **Settings → Secrets and variables → Actions → New repository secret**

Crea uno por cada variable:
| Nombre        | Valor                          |
|---------------|--------------------------------|
| `SMTP_HOST`   | smtp.gmail.com                 |
| `SMTP_PORT`   | 587                            |
| `SMTP_USER`   | tucorreo@gmail.com             |
| `SMTP_PASS`   | tu app password                |
| `EMAIL_TO`    | dónde recibir los trabajos     |

### 4. Pruébalo manualmente
- Ve a la pestaña **Actions** de tu repo.
- Selecciona "Find Remote Jobs" → **Run workflow**.
- En la primera corrida te va a llegar un email con TODOS los matches actuales
  (porque no hay nada "visto" todavía). Después solo te llegan los nuevos.

---

## 🔧 Cómo personalizar

Abre `find_jobs.py`:

- **`MUST_MATCH`** — las palabras clave que busca. Agrega/quita lo que quieras.
- **`STRONG_SIGNALS`** — términos que cuentan como match aunque estén solo en los tags.
- **`EXCLUDE`** — palabras para descartar (ej: `["principal", "staff"]` si no quieres esos niveles).

**Frecuencia:** edita el `cron` en `.github/workflows/find-jobs.yml`
- Cada 6 horas: `"0 */6 * * *"`
- Cada 12 horas: `"0 */12 * * *"`
- Una vez al día a las 9am UTC: `"0 9 * * *"`

---

## ⚠️ Notas

- **No incluí LinkedIn / Seek / Indeed** a propósito: bloquean scraping y
  rompen el bot seguido. Las 5 fuentes que usé son APIs/feeds públicos estables.
- We Work Remotely se lee vía su feed RSS (no tiene API JSON).
- El filtro "strong signal" puede traer algún Full Stack que solo menciona
  React Native de pasada. Si quieres que sea más estricto (solo matchear si
  está en el TÍTULO), cambia la función `matches()` — está comentado cómo.
