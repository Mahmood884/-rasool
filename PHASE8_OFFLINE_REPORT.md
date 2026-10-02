# تقرير تحقق Phase 8 — EventBus دون اتصال

**التاريخ:** 2026-10-01  
**النطاق:** تحقق محلي من مسار الأحداث وSafety فقط؛ لا يتضمن تشغيل Rasool
كاملاً أو اختباراً حرارياً.

## النتائج

- `system.idle` ينشره `Supervisor` مباشرة إلى EventBus، لا عبر `Safety`.
  لذلك عدم وجوده في `safety.allowlist` لا يمنع تبديل ملف `github_agent`.
- سجل الاختبار أرسل `system.idle` عبر EventBus إلى اشتراك `GitHubAgent`:
  - عند idle: `qwen2.5:3b` والحد 20.
  - عند active: `qwen2.5:1.5b` والحد 5.
- مسار `intel.item` يمر عبر `GitHubAgent._publish` ثم `Safety.check` ثم
  `EventBus.publish_sync`. اختبر subscriber حقيقي داخل الاختبار واستلم
  payload العنصر بعد نجاح الترجمة الوهمية، وسجل lifecycle الحالة `published`.
- عند استخدام allowlist فارغة، رفض `Safety` نشر `intel.item` قبل وصوله إلى
  subscriber؛ الاختبار تحقق من عدم تسليم الحدث.
- events الصحة الأساسية (`agent.health`) ينشرها `Service` مباشرة عبر
  EventBus أيضاً، ولا تمر عبر `Safety`. أما أحداث الوكلاء الخارجة مثل
  `net.*`, `intel.*`, `router.*` فتُفحص عبر بوابة Safety في مساراتها.

## التغييرات

- حُسّن `tests/test_github_lifecycle.py` ليستخدم EventBus الفعلي في اختبارات
  تسليم `intel.item` ورفضه وتبديل ملف الترجمة مع `system.idle`.
- لم يتغير تنفيذ `Supervisor`, EventBus أو Safety في هذا التحقق.
- لم تنفذ أي مكالمات HTTP أو Ollama أو التقاط حزم أو تشغيل طويل.

## التحقق

- الأمر: `.venv/bin/python -m unittest discover -s tests -v`
- النتيجة: **15 اختباراً ناجحاً**.
- `py_compile` نجح للملفات الأساسية والاختبارات.
- ملفات TOML الأساسية وإعداد الراوتر وmanifest صالحة.

## ما لم يُختبر

- لم يبدأ `github_agent` بجدولة الجمع الحقيقية، ولم يتصل GitHub أو CSDN.
- لم يُستهلك الحدث خارج العملية التجريبية بواسطة sink دائم.
- لم ينفذ تشغيل مراقب 20–30 دقيقة؛ وهو منفصل عن هذا الاختبار offline.

## متابعة Phase 8.5

أضيف لاحقاً `IntelFeedSink` واختبار Offline للتدفق من `GitHubAgent` عبر
Safety وEventBus إلى SQLite. لذلك لم تعد ملاحظة غياب sink تصف الحالة
الحالية؛ كانت صحيحة وقت تنفيذ تحقق Phase 8. يحتوي
`PHASE8_5_REPORT.md` على التفاصيل: 20 اختباراً ناجحاً إجمالاً، ولا تشغيل
كامل أو اتصالات خارجية أو اختبار حراري.
