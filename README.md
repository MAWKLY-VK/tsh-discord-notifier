<div dir="rtl">

# منبّه GeneralsGameCode في Discord

ينشر هذا السكربت في قناة Discord الخاصة بك كل جديد في مستودع
[TheSuperHackers/GeneralsGameCode](https://github.com/TheSuperHackers/GeneralsGameCode)
(المحرك المفتوح المصدر لـ C&C Generals Zero Hour):

| الحدث | ما يظهر في Discord |
|---|---|
| 🟢 طلب دمج جديد (Pull Request) | الرقم، العنوان، اسم الكاتب وصورته، التصنيفات (labels)، أول سطور من الوصف، والرابط |
| 🟣 دمج طلب في `main` | الرقم، العنوان، الكاتب، التصنيفات، والرابط |
| 📦 إصدار أسبوعي جديد (`weekly-YYYY-MM-DD`) | اسم الإصدار، أول بنود قائمة التغييرات، وروابط تحميل ملفات Generals وZero Hour |

يقرأ السكربت واجهة GitHub العامة (لا يحتاج حسابًا ولا صلاحيات على مستودع TheSuperHackers)،
ويرسل الرسائل عبر **Webhook** في Discord، ويحفظ ما نشره في ملف `state.json` حتى لا ينشر شيئًا مرتين.

هناك طريقتان للتشغيل، اختر **واحدة فقط** (تشغيلهما معًا يكرر الرسائل):

1. **GitHub Actions (موصى بها):** يعمل على خوادم GitHub كل 30 دقيقة، حتى وجهازك مطفأ.
2. **جهازك (Windows Task Scheduler):** يعمل كل 15 دقيقة ما دام جهازك مفتوحًا وأنت مسجّل الدخول.

---

## الخطوة 1: إنشاء Webhook في Discord

تحتاج صلاحية **Manage Webhooks** في السيرفر (صاحب السيرفر يملكها).

1. افتح Discord، ثم اضغط على اسم السيرفر ← **Server Settings** (إعدادات السيرفر).
2. اختر **Integrations** (التكاملات) ← **Webhooks** ← **New Webhook**.
3. سمِّه مثلًا `GeneralsGameCode`، واختر القناة التي تريد النشر فيها، ويمكنك وضع صورة له.
4. اضغط **Save Changes**، ثم **Copy Webhook URL**.

> ⚠️ **رابط الـ Webhook سرّي** مثل كلمة المرور: من يملكه يستطيع النشر في قناتك.
> لا تلصقه في أي محادثة أو ملف عام أو داخل الكود. ضعه فقط في المكان الموضح أدناه.
> إذا تسرّب، احذف الـ Webhook من Discord وأنشئ واحدًا جديدًا.

---

## الطريقة الأولى: GitHub Actions (موصى بها)

### 2. أنشئ مستودعًا خاصًا بك على GitHub

1. ادخل إلى <https://github.com/new>.
2. اسم المستودع مثلًا `tsh-discord-notifier`.
3. اختر **Public** (عام). المستودع العام يحصل على دقائق Actions مجانية بلا حد،
   ولن يظهر فيه رابط الـ Webhook لأنه محفوظ كـ Secret. (يمكنك اختيار Private أيضًا؛ انظر «ملاحظات» بالأسفل.)
4. اضغط **Create repository**.

### 3. ارفع الملفات

**من المتصفح:** في صفحة المستودع الجديد اضغط **uploading an existing file**
(أو **Add file ← Upload files**)، ثم اسحب **محتويات** هذا المجلد كلها، ومنها مجلد `.github`:

```
.github/workflows/notify.yml
.gitignore
README.md
notifier.py
install_windows_task.ps1
```

ثم اضغط **Commit changes**. تأكد بعد الرفع أن الملف `.github/workflows/notify.yml` ظاهر في المستودع.
لا ترفع `discord_webhook.txt` ولا `state.json` إن وُجدا.

**أو من الطرفية** (إذا كنت تستخدم git و GitHub CLI)، من داخل هذا المجلد:

```bash
git init -b main
```

```bash
git add .
```

```bash
git commit -m "TSH Discord notifier"
```

```bash
gh repo create tsh-discord-notifier --public --source . --push
```

### 4. أضف رابط الـ Webhook كـ Secret

1. في صفحة مستودعك: **Settings** ← **Secrets and variables** ← **Actions**.
2. اضغط **New repository secret**.
3. في **Name** اكتب بالضبط: `DISCORD_WEBHOOK_URL`
4. في **Secret** الصق رابط الـ Webhook الذي نسخته من Discord.
5. اضغط **Add secret**.

### 5. شغّله أول مرة للتجربة

1. افتح تبويب **Actions** في مستودعك. إذا ظهرت رسالة تطلب تفعيل الـ workflows فاضغط زر التفعيل.
2. اختر **TSH to Discord** من القائمة، ثم **Run workflow**.
3. في خانة `latest` اكتب `1` ثم اضغط **Run workflow**.

خلال دقيقة يجب أن تظهر في قناتك ثلاث بطاقات: آخر PR مفتوح، وآخر PR مدموج، وآخر إصدار أسبوعي.
ويُنشئ التشغيل ملف `state.json` في المستودع ويسجّل فيه كل ما هو موجود الآن على أنه «منشور».

بعد ذلك يعمل وحده كل 30 دقيقة، ولا ينشر إلا الجديد.
(إذا تركت `latest` على `0` في أول تشغيل، فسيسجّل الموجود دون أن ينشر شيئًا، وتبدأ الرسائل مع أول نشاط جديد.)

---

## الطريقة الثانية: على جهازك (Windows)

تحتاج Python 3 (موجود عندك: الأمر `py -3`).

1. أنشئ ملفًا اسمه `discord_webhook.txt` بجانب `notifier.py`، والصق فيه رابط الـ Webhook في سطر واحد.
   هذا الملف مُدرج في `.gitignore` فلن يُرفع إلى GitHub أبدًا.
2. جرّب دون نشر (يطبع الرسائل فقط):

   ```powershell
   py -3 notifier.py --dry-run --latest 3
   ```

3. جرّب النشر الفعلي (ينشر آخر عنصر من كل نوع، وينشئ `state.json`):

   ```powershell
   py -3 notifier.py --latest 1
   ```

4. أنشئ مهمة مجدولة تعمل كل 15 دقيقة في الخلفية بلا نافذة:

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File .\install_windows_task.ps1
   ```

   السجل يُكتب في `notifier.log` بجانب السكربت.

لحذف المهمة لاحقًا:

```powershell
schtasks /Delete /TN "TSH Discord Notifier" /F
```

---

## وضع التجربة (dry-run)

يقرأ GitHub ويطبع رسائل Discord بصيغة JSON بدل إرسالها، ولا يغيّر `state.json`، ولا يحتاج رابط Webhook:

```powershell
py -3 notifier.py --dry-run --latest 3
```

| الخيار | المعنى |
|---|---|
| `--dry-run` | اطبع الرسائل ولا ترسلها |
| `--latest N` | اعتبر آخر N طلبات مفتوحة وآخر N مدموجة وآخر N إصدارات «جديدة» (للتجربة) |
| `--state PATH` | مكان ملف الحالة (الافتراضي `state.json` بجانب السكربت) |
| `--webhook-file PATH` | ملف الرابط عند عدم وجود المتغير `DISCORD_WEBHOOK_URL` |
| `--log-file PATH` | اكتب السجل في ملف |

يقرأ السكربت رابط الـ Webhook من متغير البيئة `DISCORD_WEBHOOK_URL` أولًا، ثم من `discord_webhook.txt`.
ويستخدم `GITHUB_TOKEN` (أو `GH_TOKEN`) إن وُجد، وإلا يعمل دون توكن.

---

## كيف يمنع التكرار؟

- يجلب طلبات الدمج مرتبة حسب «آخر تحديث» (الفتح والدمج كلاهما تحديث)، ويجلب آخر 10 إصدارات.
- يحفظ في `state.json` أرقام الطلبات التي نشر فتحها، والتي نشر دمجها، وأسماء الإصدارات المنشورة،
  إضافة إلى «حدّ زمني» (`floor`): لا يُعتبر جديدًا إلا ما فُتح أو دُمج أو صدر بعده.
  لذلك لا يُعلَن عن طلب قديم لمجرد أن أحدًا علّق عليه.
- لا يُسجَّل الحدث إلا بعد أن يقبله Discord فعلًا؛ إذا فشل الإرسال في منتصفه، يُعاد إرسال الباقي في التشغيل التالي.
- في GitHub Actions يُحفظ `state.json` بـ commit في مستودعك، ولا يتغير الملف إلا عند نشر شيء جديد
  (أو مرة كل 12 ساعة تقريبًا لتحديث الحد الزمني).

---

## ملاحظات

- **التأخير:** GitHub لا يضمن دقة مواعيد التشغيل المجدول، وقد يتأخر التشغيل بضع دقائق، وأحيانًا أكثر وقت الزحام.
- **مستودع خاص (Private):** دقائق Actions المجانية 2000 دقيقة شهريًا، والتشغيل كل 30 دقيقة يستهلك نحو 1440 دقيقة. لا تجعله أسرع من ذلك في مستودع خاص.
  في المستودع العام يمكنك تغيير السطر `cron` في `.github/workflows/notify.yml` إلى `"*/15 * * * *"`.
- **حد واجهة GitHub:** دون توكن 60 طلبًا في الساعة لكل عنوان IP، والسكربت يستخدم طلبين تقريبًا في كل تشغيل. في Actions يُستخدم `GITHUB_TOKEN` تلقائيًا (1000 طلب في الساعة).
- **التعطيل التلقائي:** يعطّل GitHub الـ workflows المجدولة في المستودعات العامة بعد 60 يومًا بلا أي نشاط. بما أن `state.json` يُحدَّث بـ commit عند كل نشاط جديد، فلن يحدث هذا ما دام مستودع TheSuperHackers نشطًا. إن حدث، ستصلك رسالة من GitHub، وتعيد التفعيل من تبويب Actions.
- **@everyone:** الرسائل تُرسل مع `allowed_mentions` فارغة، فلا يستطيع عنوان PR أن ينادي أعضاء السيرفر.

## حل المشكلات

| المشكلة | الحل |
|---|---|
| فشل التشغيل برسالة `No Discord webhook` | الـ Secret غير موجود أو اسمه ليس `DISCORD_WEBHOOK_URL` بالضبط |
| `Discord rejected the message: HTTP 401/404` | الـ Webhook حُذف أو الرابط ناقص؛ أنشئ واحدًا جديدًا وحدّث الـ Secret |
| `rate limit reached` | انتظر حتى الوقت المذكور، أو قلّل عدد مرات التشغيل |
| فشل خطوة `Save state.json` بخطأ `403` | تأكد أن **Settings ← Actions ← General ← Workflow permissions** لا يمنع الكتابة، أو اختر **Read and write permissions** |
| لا تظهر أي رسالة بعد أول تشغيل | طبيعي إذا كان `latest` = `0`: أول تشغيل يسجّل الموجود فقط. شغّله يدويًا مع `latest` = `1` للتجربة |

</div>
