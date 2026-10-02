# Phase 10.0 — Analysis of Phase 9.2 Feed

## Classification

Classifications are based on whether the Arabic title conveys the source title and preserves its repository identifier.

| Classification | Count |
|---|---:|
| OK | 0 |
| TRANSLITERATED | 4 |
| WRONG_MEANING | 4 |
| PARTIAL | 5 |
| UNCHANGED | 2 |
| **Total** | **15** |

## Three worst examples

1. **Niko1221/Strata** — Source: `Niko1221/Strata`; output: `عندما تبدأ في شراء أي سطات، تذكري دائمًا أن تبحث عن القيمة والثروة.` The output invents an unrelated sentence.
2. **Albert-Weasker/niubigeo** — Source: `Albert-Weasker/niubigeo`; output: `عيسى إيفا`. The repository identifier is lost and replaced by unrelated Arabic text.
3. **yetone/magpie** — Source: `yetone/magpie`; output: `ولكن واحدة / الحبيرة`. Both identifier components are incorrectly rendered as Arabic words.

## Three best examples

1. **KKKKhazix/AIHOT** — Source and output are both `KKKKhazix/AIHOT`; the identifier is preserved exactly, although the title was left untranslated.
2. **NVlabs/SoL-Pi** — Source and output are both `NVlabs/SoL-Pi`; the identifier is preserved exactly, although the title was left untranslated.
3. **cdyforever/how-to-live-better** — Output: `كيف تعيش بحياة أفضل`. The intended title meaning is conveyed in Arabic, but the owner/repository identifier is not preserved.

## Classification details

- **TRANSLITERATED:** `Mak5er/AirCard`, `TheoLeeCJ/SemIf-OpenJev`, `yi1108/printfilm`, `shadcn-ui/lint`
- **WRONG_MEANING:** `Albert-Weasker/niubigeo`, `yang0/handraw-style`, `yetone/magpie`, `Niko1221/Strata`
- **PARTIAL:** `cdyforever/how-to-live-better`, `sdli1995/dlssg_for_sm86`, `ashemag/human-atlas`, `CopilotKit/openmuse`, `newliver666/apk-reverse`
- **UNCHANGED:** `KKKKhazix/AIHOT`, `NVlabs/SoL-Pi`
