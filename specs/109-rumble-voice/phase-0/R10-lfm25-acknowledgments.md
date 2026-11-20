# R10: can LFM2.5 1.2B draft spoken acknowledgments in eight languages?

Run 2026-11-20 on the Mac. Work folder with prompts, raw outputs, checker and results: `AstralDeep-wt/voice-bench-109/ack-eval/`. Nothing in the product tree was touched.

## Answer first

LFM2.5 is good enough to draft acknowledgments in English, Spanish, French, German, Mandarin and probably Arabic, if the opener rule is changed. It is not good enough in Japanese or Korean. Those two should use only the rotated generic lines.

The model's own quality is fine in the first six languages when it is asked to say what it is about to do. The problem is the spec's repeat rule. The model ignores the "last three openers" and keeps writing the same stem ("Voy a", "Je vais", "Ich werde", "Let me look into", "让我查一下", Arabic "لنبدأ في"). Under the rule as written, 8 of 14 English drafts are accepted and only 1 to 3 of 14 in Spanish, French, German, Arabic and Mandarin. Code would fall back to the generic line nearly every time.

| Language | Verdict | Why |
|---|---|---|
| en | Draft with code check, relax the opener rule | 14 of 14 lines judged good. 8 of 14 accepted as the rule stands, 14 of 14 without the opener rule. |
| es | Draft with code check, relax the opener rule | 14 of 14 good. 2 of 14 accepted, 14 of 14 without the opener rule. |
| fr | Draft with code check, relax the opener rule, watch for wrong details | 10 of 14 good. The 4 bad lines insert a wrong person or day ("mon emploi du temps demain") or are bare infinitives. The checker cannot see that. |
| de | Draft with code check, relax the opener rule | 13 of 14 good. 3 of 14 accepted, 12 of 14 without the opener rule. |
| zh | Draft with code check after a native review, relax the opener rule | 11 of 14 good by my reading. 2 of 14 accepted, 13 of 14 without the opener rule. |
| ar | Generic lines only until a native speaker reviews it | 12 of 14 look acceptable to me, but every line is the same stiff stem and I am not confident about naturalness. |
| ja | Generic lines only | 2 of 14 good. Many lines start with a stray "明日" (tomorrow) and fragments such as "明日読の...". The checker accepted 4 bad lines. |
| ko | Generic lines only | 0 of 14 good. The checker accepted 11 of 14 lines and every one was bad. |

The decision to relax the rule is the owner's. Two options to test later: compare only the whole opening clause with a stricter list of stems, or let code pick the opening stem from a rotated list and pass it to the model as the required start. Neither was run here.

## What the code check cannot do

Code accepted lines that were bad. With variant 1, 10 of 14 Spanish, 10 of 14 Arabic, 7 of 14 German and 7 of 14 Korean lines were accepted and all of them were wrong, ungrammatical or answered the question. With the chosen prompt the same happened to 11 of 14 Korean lines. Examples that passed every rule: "The guidelines recommend careful selection for older adults." (English), "建议使用低盐饮食和适量运动。" (medical advice in Mandarin), "I can't access your emails directly." (a false limitation).

The six rules in section 8.2 catch form. They do not catch an answer in disguise, a false statement, a wrong detail or bad grammar. This matters most for the clinical requests. Any language that ships a model draft needs a shape rule on top, for example a short allow-list of opening verb stems per language. I did not test that.

## Method

**Requests.** 20 per language, 160 in total, in `requests.py`. The same 20 intents, written natively in each language and not word for word. Intents 0 to 13 are normal: weather, a fact, a history fact, calendar read, calendar write, read email, send email, find a file, web search, medication side effects, normal heart rate, a hypertension guideline, a multi-step travel request, a multi-step email request. Intents 14 to 17 are the private or technical ones (a phone number, a street address, an API key, a patient record number). Intents 18 and 19 are a greeting and a thanks.

Pass rates below are over the 14 normal intents per language (112 in all). Code decides the private and small talk cases before the model is asked, so the 6 other intents per language are recorded in the raw files and left out of the headline rates.

**Runtime.** **Measured**: llama.cpp b12549 stock build from `runtime-mac/build-stock`, `llama-completion`, LFM2.5-1.2B-Instruct Q4_K_M, `--jinja`, `-n 24`, `-c 2048`, `-ngl 99`, `--no-warmup`, one process per draft, run one after another. Temperature 0, and 0.7 with top-p 0.9 and a fixed seed per intent.

**Feeding the openers.** Per language the 20 prompts run in order. The "last three openers" given to the model are the openers (first two words, first three characters for Chinese and Japanese) of the model's own previous lines that the checker accepted. Rejected lines are not fed back because the app would not have spoken them. Private and small talk lines are not fed back.

**Prompt selection.** Three variants, tried on 32 prompts (intents 0, 3, 9, 12 in all eight languages) at both temperatures:

| Variant | Idea | Sample pass t=0 | Sample pass t=0.7 |
|---|---|---|---|
| 1 | Plain rules: one sentence, at most 8 words, do not answer, no digits, no question, avoid the openers | 22 of 32 | 20 of 32 |
| 2 | Role and tense: say you are starting on it, in the present or future tense, with two example phrases in English | 14 of 32 | 17 of 32 |
| 3 | The same rules plus three style examples written in the target language | 16 of 32 | 18 of 32 |

By code pass rate alone variant 1 wins. I read the lines before choosing. Variant 1 passes mostly because it varies its first words, and many of those lines restate the request, answer it, or ask the user to do the task. Variant 2 writes natural "I will check X" lines in the Latin languages and Chinese and loses only to the repeat rule. Variant 3 copies its examples, sometimes writes two lines, and is cut off in Arabic and Korean.

I ran all 160 prompts for variants 1 and 2 at both temperatures so the choice can be checked. **The chosen prompt is variant 2 at temperature 0.7.** This is a judgment, not the score winner. By my count it gives 20 lines that are both accepted and good across the 112 normal prompts, against 16 for variant 1. I judged 76 of its 112 lines good, against 20 for variant 1.

Temperature 0.7 against 0: 36 against 31 accepted of 112 for variant 2, 70 against 59 for variant 1. One run per setting, so treat the difference as small.

### Chosen prompt, verbatim

System message, with `{Language}` replaced by the English name of the session language (English, Spanish, French, German, Arabic, Mandarin Chinese, Japanese, Korean):

```
You are the voice of an assistant. The user just spoke a request and the answer is still being prepared. Say a brief natural filler in {Language} that tells the user you are starting on it, in the present or future tense (like 'let me look into that' or 'one moment'). Rules: one sentence, at most 8 words, never answer or hint at the answer, never say anything is finished, found or sent, no numbers, no question, no quotation marks. Begin differently from the recent openers. Write only the sentence, in {Language}.
```

User message:

```
Session language: {Language}
Recent openers: {up to three openers joined by a semicolon and a space, or "none"}
User request: {the transcript}
```

Variants 1 and 3 are in `prompts.py`.

### Rules the prototype checker applies

`checker.py`. A draft is accepted only if no rule fails. The text is first normalized with NFKC, the end-of-text marker is removed, and surrounding quote marks are trimmed.

1. **Finished.** The model stopped on its own and did not hit the 24 token limit.
2. **One line.** No line break.
3. **One sentence.** After removing trailing sentence marks, none of `。 ！ ？ . ! ? ؟ ‽ …` remains inside.
4. **Length.** At most 12 words (whitespace split) for English, Spanish, French, German, Arabic and Korean. For Chinese at most 18 characters and for Japanese at most 28 characters, counting letters only. I chose these as roughly 3.5 to 4 seconds of speech at about 5 and 7 characters per second. **Assumed**, not measured.
5. **No question.** None of `? ？ ؟ ⁇ ⁈ ⁉ ¿`.
6. **No markup.** No brackets, asterisks, backticks, hash marks or double quote characters.
7. **No digits.** No character of Unicode category Nd, No or Nl. Also no spelled numbers above one in each language (two to ten, hundred, thousand, zero and the like, including Chinese and Japanese numerals and Korean native numbers). "One", "un", "一" and "ein" are allowed so that "un momento" and "我来查一下" pass. Weekday words such as 周五 are exempted after a false hit.
8. **No claim of something done, found or sent.** A per-language list of perfect and past forms and completion words. English: "I've", "I found", "I sent", "(I|we) verb-ed" except "need", "done", "found", "sent", "here is", "already", "ready" and the like. Spanish: "ya", "he", "hemos", "listo", "encontré", "enviado" and the like. French: "j'ai", "déjà", "voici", "c'est", "envoyé", "trouvé" and the like. German: "habe", "hier ist", "schon", "ist", "sind", "gefunden", "gesendet", and any "ge...t" or "ge...en" participle. Arabic: "تم", "وجدت", "أرسلت", "لقد", "قد", "بالفعل", "إليك" and the like, with diacritics removed. Chinese: "了", "已经", "找到", "完成", "搞定", "这是", "好了" and the like, except "了解". Japanese: "ました", "でした", "完了", "済み", "見つか", "こちらです" and the like, except the receipt phrases "かしこまりました", "承知しました", "了解しました", "わかりました". Korean: any syllable with the past final consonant ㅆ except "겠", "있" and "없", plus "완료", "끝났", "여기".
9. **Language.** Script check first. Spanish, French, German and English must be all Latin letters and Apple's NaturalLanguage recognizer, limited to the eight session languages, must name the session language as its top guess. Arabic must be all Arabic script. Chinese must be all Han with no kana or Hangul. Japanese must contain kana and no Hangul. Korean must be all Hangul. A first attempt with a stop-word list rejected valid Spanish, so I replaced it with the recognizer. The recognizer is a compiled helper, `nlid.swift`.
10. **Opener.** The first two words (first three characters for Chinese and Japanese, punctuation removed, lower case) must not equal any of the last three openers.

Known holes. The claim lists are not complete. Spanish "llegué" and German "ist" show the two kinds of error: a past form the list misses and a plain "is" that is rejected only because I listed it. There is no check that the line answers, that it is true, or that it fits the request.

## Pass rates

Chosen prompt (variant 2, temperature 0.7), 14 normal intents per language. "Quality good" is my judgment of the 14 lines.

| Lang | Code accepted | Accepted without opener rule | Quality good | Accepted and good | Accepted but bad |
|---|---|---|---|---|---|
| en | 8 | 14 | 14 | 8 | 0 |
| es | 2 | 14 | 14 | 2 | 0 |
| fr | 3 | 14 | 10 | 2 | 1 |
| de | 3 | 12 | 13 | 3 | 0 |
| ar | 1 | 13 | 12 | 1 | 0 |
| zh | 2 | 13 | 11 | 2 | 0 |
| ja | 6 | 11 | 2 | 2 | 4 |
| ko | 11 | 11 | 0 | 0 | 11 |
| all | 36 of 112 | 102 of 112 | 76 | 20 | 16 |

Same prompt at temperature 0: accepted 8, 1, 2, 2, 1, 3, 8, 6 (31 of 112). Without the opener rule 14, 14, 14, 13, 14, 13, 12, 9.

Variant 1 at temperature 0.7, for comparison: accepted 13, 10, 7, 7, 10, 7, 9, 7 (70 of 112). Quality good 9, 0, 7, 0, 0, 1, 3, 0. Accepted and good 9, 0, 3, 0, 0, 1, 3, 0. Variant 1 at temperature 0: accepted 10, 11, 3, 7, 8, 4, 6, 10 (59 of 112).

Failure counts over the 112 normal lines for the chosen run: repeat opener 69, claim 4, truncated 4, multi-sentence 3, too long 1, digits 1. For variant 1 at 0.7: digits 15 (the model answers "1989" or "8,848"), repeat opener 12, claim 8, truncated 6, multi-line 6, multi-sentence 6, question 5, too long 2.

Private and small talk lines (32 and 16 for each variant, informational). With the chosen prompt the model produced a code-accepted line for 7 of 32 private requests and 4 of 16 small talk. For example "I'll save that key for you right away." for an API key. Code must stop these before the model is asked, as the spec says. For small talk the model never produced a sensible reply (English "Let me start this task right away." for a greeting).

My reading of the lines. I am confident in English, Spanish, French and German. I am fairly confident in Mandarin. In Arabic, Japanese and Korean I can see wrong, ungrammatical and off-topic lines reliably but cannot judge natural register. Treat the Arabic "acceptable" count as the least certain figure and the Japanese and Korean failures as certain. A native review of at least Arabic and Mandarin is still needed.

## Example lines

Chosen prompt unless marked v1. "Opener" means rejected only by the repeat rule.

**English.** Good: "Let me look into that." Good: "I’ll check your schedule right away." Good: "I will review your unread emails and prepare concise replies." Good but opener: "I will guide you there step by step." Bad (v1): "I can't access your emails directly." (false) Bad (v1): "Mount Everest stands at impressive heights." (answers)

**Spanish.** Good: "Voy a investigar el pronóstico para mañana." Good but opener: "Voy a leer los correos sin leer de mi jefa." Good but opener: "Voy a enviar el correo al equipo pronto." Bad (v1): "Leo los correos sin leer tu jefe." (ungrammatical) Bad (v1): "Encuentra vuelos y comparan con tren."

**French.** Good: "Je vais vérifier la prévision météo pour vous." Good: "Laissez-moi vérifier cette information pour vous." Good but opener: "Je vais envoyer le mail à l'équipe." Bad: "Je vais vérifier mon emploi du temps demain." (wrong person and day) Bad: "Commencer par explorer cette question maintenant."

**German.** Good: "Ich werde das Wetter morgen in Lexington genauer prüfen." Good: "Lass mich das herausfinden." Good but opener: "Ich werde die E-Mails sorgfältig lesen." Bad: "Ich werde prüfen, was Erwachsene normal haben." Bad (v1): "Bitte schicke das Team die Nachricht."

**Arabic.** Acceptable to me: "لنبدأ في التحقق من التقويم" ("let us begin checking the calendar"). Acceptable: "لنبدأ في ترتيب الاجتماع مع آنا يوم الجمعة." Bad: "لنبدأ في قراءة الرسائل المهملة." (junk messages) Bad (v1): "الطقس سيسري غداً في ليكسلنجتون." (meaningless) Bad (v1): "الجبل عالي جداً." (answers)

**Mandarin.** Good: "让我查一下明天的天气预报" Good: "我来查看一下上周修改过的预算表格。" Good: "让我查一下下周一的航班和火车方案。" Bad: "我会听你说的那些话了。" Bad (v1): "建议使用低盐饮食和适量运动。" (medical advice, passed the checker)

**Japanese.** Good (v1): "ガイドラインを確認中です。" Good (v1): "天気情報を確認中です。" Good: "明日レキシントンの天気を確認しましょう。" Bad: "明日読の連絡を859 555 0142に送りましょう。" (garbled, repeats the number) Bad (v1): "日本語で答えます。" (irrelevant)

**Korean.** Least bad: "지금 이 문제를 조사하겠습니다." (generic and fine, but it came from a private request). Bad: "내일 렉싱턴 날씨가 괜찮을 것 같아요." (guesses the forecast) Bad: "메트포르민의 일반적인 부작용은 알코올을 피해야 합니다." (answers, wrongly) Bad: "오늘 이메일을 내일에 보내겠습니다." Bad (v1): "네, 가까운 채식 식당 찾으세요." (tells the user to do the search)

## Failure patterns

- **Stem repetition.** With variant 2 the model reuses one opening stem per language regardless of the openers it is given. This is the main reason the numbers are low.
- **Answering.** With plain rules (variant 1) the model often answers the question or restates it as a question. The digits rule catches the numeric answers in the Latin languages and Chinese. It does not catch "Mount Everest stands at impressive heights."
- **Imperatives to the user.** In German, Japanese, Korean and Chinese variant 1 often writes "Check your calendar" or "Please tell me about side effects", reversing the roles.
- **Hallucinated time words.** Japanese lines start with "明日" and fragments such as "明日読", and Korean and French lines insert "tomorrow" or "today" when the request had none.
- **Truncation and second lines.** In Arabic, Korean and Japanese the model sometimes writes a second line or is cut at 24 tokens, which can split a character. The runner decodes with replacement and the checker rejects these.
- **Echoed private details.** 3 of 32 private requests produced a line containing the number or key. The model must not be asked at all for those.

## Timing, indicative only

**Measured**, Apple M5, macOS 27.2.1, llama.cpp b12549 stock Metal build, `-ngl 99`, a fresh process per draft, another agent possibly using the GPU at the same time. These say nothing about the iPhone.

Chosen prompt, temperature 0.7, 160 drafts: mean 11.7 tokens generated per draft (max 23), generation 7.5 ms per token (about 134 tokens per second), wall time per draft mean 0.38 s and max 1.0 s. The wall time includes starting the process, mapping the model and compiling Metal. An app that keeps the model loaded would see mostly prompt processing plus generation, but that was not measured. Other settings gave 8 to 8.6 ms per token and 0.43 to 0.47 s mean wall time.

## What remains unknown

- Whether an iPhone and a resident model reach the "ready before the turn ends" timing in section 8.2. Nothing here measures the phone.
- Whether a code-chosen opening stem, or a shape rule per language, makes the Latin languages and Mandarin usable at a high accept rate. Not run.
- Native-speaker quality for Arabic, Mandarin, Japanese and Korean, and whether Japanese and Korean improve with a larger local model.
- Behavior from a partial transcript that the final transcript later changes. All prompts here used complete requests.
- Variation across seeds. Each setting was one run, so differences of a few lines between temperatures or prompts are inside the noise.
- Whether the first-two-words rule should look at the opener at all, given that the generic fallback lines are themselves rotated.
