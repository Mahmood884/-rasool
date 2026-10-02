# تقرير المرحلة السابعة — الأمان والرصد التشغيلي

**التاريخ:** 2026-10-01  
**النطاق:** حماية سر الراوتر، جعل Safety يرفض افتراضياً، وإضافة سجل دائم
لدورة حياة عناصر GitHub.

## الملخص

نُقلت كلمة مرور الراوتر من TOML إلى ملف محلي بصلاحيات المستخدم فقط، وأصبح
الوكيل يرفض الأسرار inline ويتحقق من ملكية ملف السر وصلاحياته. تغيّر
`Safety` ليمنع كل إجراء غير موجود في allowlist، مع تصريح صريح بأحداث الوكلاء
في الإعداد الأساسي. أضيف جدول SQLite لحالة كل عنصر GitHub مع عدادات دورية
في السجل. أُنشئ الجدول في قاعدة الحالة الموجودة دون تعديل صفوف الطابور
القائمة. لم يبدأ Rasool ولم تُرسل طلبات إلى الشبكة في هذه المرحلة.

## الملفات المعدّلة

- `/home/sol1/rasool/agents/router_agent/agent.py` — تحميل كلمة المرور من
  ملف محمي والتحقق من نوع الملف والمالك والصلاحيات.
- `/home/sol1/rasool/agents/router_agent/config.toml` — إزالة حقل كلمة
  المرور وإضافة مسار ملف السر.
- `/home/sol1/rasool/agents/router_agent/manifest.toml` — إزالة قيمة كلمة
  المرور الافتراضية واستبدالها بتوثيق مسار ملف السر.
- `/home/sol1/rasool/rasool/safety.py` — منع افتراضي عند allowlist فارغة.
- `/home/sol1/rasool/config.toml` — قائمة صريحة بأحداث الوكلاء المسموح بها.
- `/home/sol1/rasool/agents/github_agent/agent.py` — تخزين انتقالات الحالة
  والنموذج والمحاولات والأخطاء المختصرة في SQLite، وسجل عدادات الحالات.
- `/home/sol1/rasool/tests/test_safety.py` — اختبارات deny-by-default.
- `/home/sol1/rasool/tests/test_router_secret.py` — اختبارات حماية سر الراوتر.
- `/home/sol1/rasool/tests/test_github_lifecycle.py` — اختبارات تدفق الحالة.
- يتضمن اختبار GitHub تحقق ترويسة Bearer باستخدام token اختباري وهمي فقط؛
  لم يُقرأ token الحقيقي لهذا الاختبار.
- `/home/sol1/rasool/SYSTEMS_ENGINEERING_REPORT.md` — تحديث التقرير الهندسي.
- `/home/sol1/rasool/PROJECT_STATE.md` — تحديث حالة المشروع.
- `/home/sol1/rasool/agents/router_agent/README.md` — توثيق إعداد سر الراوتر.
- `/home/sol1/rasool/agents/github_agent/README.md` — توثيق جدول التتبع.

## سر الراوتر

- مسار السر: `/home/sol1/.config/rasool/router_agent/password`.
- الصلاحيات المتحققة: `0600`، والمالك هو المستخدم الحالي.
- لم تُطبع القيمة أثناء النقل أو الاختبار، ولم تُكتب في أي تقرير.
- مجلد السر: `~/.config/rasool/router_agent` بصلاحية `0700`.
- يرفض الوكيل الملف غير العادي، والملف غير المملوك للمستخدم، وأي صلاحية
  للمجموعة أو الآخرين، وكذلك حقل `password` inline في TOML.
- أزيلت قيمة السر من إعداد الوكيل وmanifest؛ فحص ملفات المشروع لم يجد نسخاً
  نصية أخرى.
- تأكد TOML الراوتر وmanifest خاليين من حقول كلمة مرور inline.

## سياسة Safety

- أصبحت نتيجة `Safety.check(action, payload)` صحيحة فقط إذا كان `action`
  ضمن allowlist.
- القائمة الفارغة تعني رفض كل إجراء، وليس السماح الكامل.
- `config.toml` يصرّح حالياً بهذه الأحداث: `agent.error`, `agent.health`,
  `intel.batch`, `intel.item`, `net.arp`, `net.dns`, `router.client`,
  `router.session`, `router.unknown`.
- الأحداث التي لا تظهر في القائمة ستُرفض؛ يجب تحديثها صراحة عند إضافة
  مخرجات وكلاء جديدة.

## رصد دورة حياة GitHub

الجدول الجديد `item_lifecycle` في
`/home/sol1/.cache/rasool/github_agent/queue.sqlite` يسجل:

- `item_id`: SHA-256 للرابط، ويستخدم مفتاحاً ثابتاً.
- `source`, `url`, `status`.
- `attempts`, `last_error`, `next_retry_at`.
- `model_used`, `created_at`, `updated_at`, `published_at`.
- `metadata_json`: بيانات العنصر اللازمة للاستمرار والتشخيص.

الحالات المسجلة: `fetched`, `queued`, `translated`, `published`,
`retry_wait`, `failed`. حالة `failed` لا تحذف العنصر؛ عند بلوغ حد المحاولات
يظل في طابور الترجمة ويعاد جدولته أسبوعياً وفق السلوك السابق. يخزن `last_error`
مرحلة الخطأ ونوع الاستثناء فقط، لا الرسالة التفصيلية، لتقليل خطر حفظ بيانات
حساسة في SQLite.

يستمر جدول `translation_queue` بوظيفته الحالية. التهيئة أضافت الجدول والفهرس
فقط ولم تحذف صفوف الطابور القائمة. لم تُحوّل العناصر التاريخية تلقائياً إلى
`item_lifecycle`؛ يبدأ التتبع للعناصر التي يعالجها الكود الجديد. تكتب
عدادات الحالات إلى سجل التطبيق عند دورات الجمع/المحاولة، ويمكن فحص الحالة
باستعلام SQLite:

```sql
SELECT status, COUNT(*)
FROM item_lifecycle
GROUP BY status
ORDER BY status;
```

## التحقق

- `python -m unittest discover -s tests -v`: **13 اختباراً ناجحاً**.
- `py_compile` نجح للملفات المعدّلة والاختبارات.
- TOML الأساسي وTOML الراوتر صالحان.
- حقل كلمة المرور inline غير موجود في إعداد الراوتر.
- تهيئة قاعدة SQLite نجحت؛ جدول `item_lifecycle` موجود وبدأ بـ0 صفوف.
- smoke check أنشأ `RouterAgent` من إعداد التشغيل وقرأ ملف السر بنجاح،
  وعرض نتيجة boolean فقط دون عرض قيمة السر.
- `systemctl --user is-active rasool.service`: `inactive`.
- لم يُشغّل اختبار HTTP أو التقاط حزم أو ترجمة؛ لا توجد آثار خارجية لهذا
  التحقق.

## ملاحظات وحدود

- عناصر قاعدة البيانات القائمة تظل في `translation_queue`، لكنها لا تظهر
  في lifecycle حتى يعالجها الوكيل بالتغيير الجديد.
- `published` يعني أن `publish_sync` اكتمل ضمن EventBus؛ يلزم اختبار تكامل
  مع subscriber حقيقي للتحقق من استهلاك الحدث end-to-end.
- allowlist الحالية تصرّح بمجموعة أحداث الوكلاء المعروفة. عند إدخال topic
  جديد لن يعمل نشره حتى تتم إضافته صراحة إلى الإعداد.
- لم تُعدّل العتبات الحرارية، أو `thermal.py`، أو وحدة systemd، ولم يبدأ
  الوكيل بعد التغييرات.
