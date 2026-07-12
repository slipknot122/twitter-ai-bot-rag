# Twitter AI Bot RAG — Project Rules & Workflow

## Project Context
You are working on an early-stage Python application that ingests Telegram
messages, rewrites them with an LLM, stores drafts in SQLite, optionally
generates media, lets a human review drafts in a local FastAPI admin panel,
and publishes approved posts to X/Twitter.

Repository: https://github.com/slipknot122/twitter-ai-bot-rag

---

## Important Constraints
- This is currently a local, single-process application.
- The admin panel must remain bound to 127.0.0.1.
- Never expose or modify secrets through the web admin.
- Never print or commit .env values, Telegram session data, database files,
  generated media, or private logs.
- Preserve the existing architecture unless a change is required.
- Use parameterized SQL only.
- Keep SQLite WAL behavior.
- Avoid large rewrites.
- Do not change multiple subsystems in one commit.
- Preserve DRY RUN as the default.
- Full Auto must not be enabled by default.
- Image-generation failure must never invalidate a valid text draft.
- Ambiguous X/Twitter publishing failures must never be blindly retried.
- Do not add new dependencies unless necessary.
- Do not edit requirements.txt until a dependency has actually been added.
- Use UTC-aware datetimes.
- Add tests for every behavior changed.
- Do not claim success based only on syntax compilation.

---

## Required Workflow
1. Inspect the entire relevant code path before editing.
2. Produce a short implementation plan and list affected files.
3. Identify assumptions and risks.
4. Wait for approval before making changes.
5. Implement one phase at a time.
6. After each phase:
   - show the diff summary;
   - run tests;
   - run Python compilation;
   - explain remaining risks;
   - stop and wait for approval before the next phase.

Do not implement the complete audit in one pass.

---

## Strict Phase Execution Rules
1. Спочатку проаналізуй актуальний стан усього репозиторію.
2. Звір кожен пункт аудиту з поточним кодом.
3. Познач кожен пункт: Confirmed / Already fixed / Partially implemented / Not reproducible.
4. На першому кроці не змінюй жодного файлу.
5. Покажи план лише для поточної фази: точні файли, конкретні зміни, ризики, тести, критерії приймання.
6. Зупинись і дочекайся підтвердження.
7. Після підтвердження реалізуй тільки поточну фазу.
8. Не переходь до наступної фази автоматично.
9. Після кожної фази:
   - покажи git diff;
   - запусти всі тести;
   - запусти compileall;
   - перевір змінені сценарії вручну або інтеграційними тестами;
   - перевір, що секрети не потрапили у diff чи логи;
   - поясни результати;
   - створи окремий коміт;
   - зупинись і дочекайся дозволу.
10. Якщо тест не проходить — не переходь далі.
11. Якщо вимога суперечить актуальному коду — спочатку запитай.
12. Не роби великих рефакторингів без необхідності.
13. Не змінюй сторонні частини проєкту.
14. Не вмикай Full Auto за замовчуванням.
15. DRY RUN і Shadow Mode повинні залишатися безпечними дефолтами.
16. Не публікуй реальні пости під час тестування.
17. Не виконуй запити, які можуть надіслати повідомлення або створити пост.
18. Не відкривай адмінку за межами 127.0.0.1.
19. Не показуй, не логуй і не коміть секрети.
20. Не читай та не виводь значення .env у відповідь.
21. Не коміть файли: .env, *.session, *.db, *.db-wal, *.db-shm, generated media, logs.
22. Використовуй mocks/fakes для Gemini, Telegram, X і media providers.
23. Перед додаванням dependency поясни навіщо вона потрібна.
24. Для кожної зміненої поведінки додай pytest-тест.
25. Не вважай compileall достатньою перевіркою.
26. Не видаляй існуючі дані SQLite.
27. Усі міграції мають бути backward-compatible та idempotent.
28. Використовуй UTC-aware datetime.
29. Використовуй parameterized SQL.
30. Зберігай поточну single-process SQLite архітектуру, доки план прямо не вимагає іншого.

---

## Phase Order
- **Phase 0** — Audit verification only, no file changes.
- **Phase 1** — Shadow Mode, temperature, Telegram status, media fallback, text validation, settings validation.
- **Phase 2** — SQLite integrity and state machine.
- **Phase 3** — Independent Post Auditor.
- **Phase 4** — Automatic media pipeline.
- **Phase 5** — X publishing duplicate protection.
- **Phase 6** — Source reputation and analytics.

---

## Phase Completion Checklist
After completing each phase, before moving on:
1. Покажи список змінених файлів.
2. Покажи короткий diff summary.
3. Покажи результати pytest і compileall.
4. Перевір git status.
5. Перевір, що у diff немає секретів.
6. Перевір acceptance criteria по одному.
7. Назви залишкові ризики.
8. Якщо все пройшло — створи окремий коміт цієї фази.
9. Зупинись.

---

## Do Not Give Gemini Large Patches
Do not paste large ready-made code patches. Let the agent inspect HEAD first,
then propose minimal changes for the current phase only.

Provide: repository, audit text, master prompt, phased plan, acceptance criteria,
small pseudocode examples. Not full implementations.
