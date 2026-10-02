# Phase 9.2 — تقرير تقوية الإلغاء والتشغيل

**التاريخ:** 2026-10-01  
**المشروع:** `/home/sol1/rasool`

## Summary

أُضيفت اختبارات للنافذتين المتبقيتين لإلغاء `github_agent` أثناء تفريغ
الطابور وأثناء النشر؛ اجتاز ملف الاختبارات المركّز 6 اختبارات، واجتاز
المشروع كاملاً 27 اختباراً، كما نجح `py_compile` وفحص ثبات الإعداد الدائم.
في التشغيل المحدود لمدة خمس دقائق، وصلت 15 مادة GitHub إلى
`published` وحُفظت 15 مادة في `intel_feed`، من دون بلوغ حد الإيقاف
الخارجي عند 82°C. لم يتغير `agent.py` أو `config.toml` خلال هذه المتابعة.

## Part A — Code changes

بنية `_process_item` ذات الصلة بعد التغيير:

```python
record lifecycle = fetched
try:
    translated = await _translate_record(record)
except CancelledError:
    shield queue_record(..., last_error="cancelled:shutdown")
    shield record_item_state(..., status="retry_wait",
                             last_error="cancelled:shutdown")
    raise

if translated is None:
    try:
        await _queue_record(record)
    except CancelledError:
        await _shielded_queue_on_cancel(record, "cancelled:queue_flush")
        raise
    return

record lifecycle = translated
try:
    await _publish_item(record, translated)
    record lifecycle = published
except Exception:
    queue record and emit item_publish error
except CancelledError:
    await _shielded_queue_on_cancel(record, "cancelled:publish_flush")
    raise
```

تأكد اختبار introspection من وجود قيم الإلغاء الثلاث المتميزة داخل
`_process_item`: `cancelled:shutdown`, `cancelled:queue_flush`,
`cancelled:publish_flush`.

## Part B — New tests

- `test_cancelled_during_queue_flush_is_rescheduled`
- `test_cancelled_during_publish_flush_is_rescheduled`
- `test_three_cancellation_windows_are_all_protected`
- نتيجة ملف الاختبار المركّز: **ناجح — 6 اختبارات من أصل 6**.

## Part C — Verification

- الاختبار المركّز باستخدام unittest discovery: **6 ناجحة، 0 فاشلة**.
- الاختبار الكامل باستخدام unittest discovery: **27 ناجحة، 0 فاشلة**.
- فحص `py_compile` للملفات المحددة: **نجح**.
- فحوص الإعداد الدائم: **نجحت**؛ `throttle_at=78.0`,
  `suspend_at=85.0`, و`enabled_agents` بقيت
  `["sniff_agent", "github_agent", "router_agent"]`.
- خدمة systemd بقيت غير نشطة.

## Part D — Operational test

- الإعداد المؤقت فعّل `github_agent` فقط، مع `IntelFeedSink` على مسار
  feed المعتاد. لم يُعدّل `config.toml` أو `thermal.py`.
- المدة: **300.1 ثانية**، من `2026-10-01 11:28:52` إلى
  `2026-10-01 11:33:52`.
- انتهى الاختبار بمهلة الخمس دقائق (`timeout`, status 124)، وليس بإيقاف
  حراري. أعلى عينة خارجية كانت **81°C**؛ لم تبلغ 82°C. أظهرت آخر عينة
  62°C.
- القياسات من السجل:
  - GitHub repository search HTTP 200: **3**.
  - Ollama `/api/generate` HTTP 200: **35**.
  - ظهور `cancelled:` في السجل: **0**.
  - انتقالات حرارية: **13**.
  - بدء/إيقاف `github_agent`: **3 / 3**.
- عدد صفوف `translation_queue`: **1 قبل → 2 بعد**.
- عدد صفوف `item_lifecycle`: **1 قبل → 17 بعد**. عند النهاية:
  `published=15`, `retry_wait=2`; وكانت الحالة الوحيدة قبل التشغيل
  `fetched=1`.
- عدد صفوف `intel_feed`: **0 قبل → 15 بعد**، وكل الصفوف الخمسة عشر
  من `github_trending`.
- بقي عنصران في الطابور عند النهاية: عنصر `csdn_ai` بحالة إعادة محاولة
  بعد خطأ ترجمة، وعنصر GitHub بحالة `retry_wait` وسبب
  `cancelled:shutdown`. لا يوجد عنصر GitHub `published` بلا صف feed
  ضمن نتائج هذا التشغيل.

العناوين العربية (مقتطف أول 40 محرفاً) للعناصر المحفوظة:

| URL | العنوان العربي |
|---|---|
| `https://github.com/Mak5er/AirCard` | ماك5ر / ايركارد |
| `https://github.com/cdyforever/how-to-live-better` | كيف تعيش بحياة أفضل |
| `https://github.com/Albert-Weasker/niubigeo` | عيسى إيفا |
| `https://github.com/TheoLeeCJ/SemIf-OpenJev` | عولو لي جاي // سيم إف إس فونج إيفج |
| `https://github.com/KKKKhazix/AIHOT` | KKKKhazix/AIHOT |
| `https://github.com/sdli1995/dlssg_for_sm86` | sdli1995/السجلات للsm86 |
| `https://github.com/ashemag/human-atlas` | الشماغ/ atlas لليوم humano |
| `https://github.com/yi1108/printfilm` | يوليو 8/فيلم |
| `https://github.com/yang0/handraw-style` | العربية: يمنا0/ style |
| `https://github.com/yetone/magpie` | ولكن واحدة / الحبيرة |
| `https://github.com/Niko1221/Strata` | عندما تبدأ في شراء أي سطات، تذكري دائمًا |
| `https://github.com/CopilotKit/openmuse` | -kit/ومو موس |
| `https://github.com/NVlabs/SoL-Pi` | NVlabs/SoL-Pi |
| `https://github.com/newliver666/apk-reverse` | لعبة تحمل نسق تطوير APK.reverse |
| `https://github.com/shadcn-ui/lint` | شادني-ui/لنتش |

الإجابات المطلوبة:

**(a)** نعم؛ وصلت عناصر إلى الحالة `persisted` (15 صفاً في feed).  
**(b)** نعم؛ مثلاً `https://github.com/Mak5er/AirCard` بعنوان
`ماك5ر / ايركارد`. القائمة الكاملة أعلاه.  
**(c)** لا ينطبق؛ وجدت عناصر محفوظة. بقي عنصر GitHub آخر في
`retry_wait` بسبب `cancelled:shutdown`.  
**(d)** لا؛ لم يظهر نص `cancelled:` في سجل التطبيق، مع أن قاعدة lifecycle
سجلت سبب الإلغاء للعنصر المؤجل عند الإنهاء.

## Deviations

- محاولة التشغيل الأولى انتهت خلال 5 ثوانٍ برمز 127 قبل إنشاء سجل التطبيق،
  لأن المشغّل نُفّذ من خارج مجلد المشروع ولم يُعثر على `./run.sh`. لم يبدأ
  Agent ولم تتغير قاعدة البيانات في تلك المحاولة. أُعيد تشغيل المشغّل
  نفسه من `/home/sol1/rasool`، واكتمل اختبار الخمس دقائق.
- انتهى التشغيل عند المهلة المحددة؛ لم يُوقفه حد الحرارة الخارجي.

## Notes

- خلال التشغيل المكتمل وصل 15 عنصراً إلى `published` وظهر لها حفظ محلي
  في `intel_feed`؛ كما ظهر عنصر آخر في `retry_wait` عند إلغاء الوكيل
  أثناء الإنهاء.
- لم تُعدّل عتبات الحرارة أو قائمة الوكلاء الدائمة، ولم تُشغّل خدمة
  systemd.
- أزيلت ملفات الإعداد والسجل والعينات المؤقتة بعد جمع القياسات.
