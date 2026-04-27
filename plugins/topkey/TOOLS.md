# TOOLS.md — TopKey HR & Project Management

Siz TopKey tizimiga (https://topkey.uz, Laravel REST API) ulangansiz. 28 ta `topkey_*` tool mavjud.

**MUHIM:** Login ma'lumotlari OLDINDAN sozlangan. Foydalanuvchidan email, parol yoki API URL HECH QACHON so'ramang. Token avtomatik yangilanadi (401 bo'lsa re-login).

## HR — Xodimlar (5)
- `topkey_list_employees` — xodimlar ro'yxati. Filter: `department_id`, `designation_id`, `status`. Paginatsiya: `page`, `per_page`.
- `topkey_get_employee` — bitta xodimning to'liq profili (`employee_id`).
- `topkey_list_departments` — bo'limlar.
- `topkey_list_designations` — lavozimlar.
- `topkey_get_user_profile` — joriy autentifikatsiya qilingan foydalanuvchi profili (\"men kim?\").

## HR — Davomat (5)
- `topkey_get_today_attendance` — bugun (yoki `date` parametrida) check-in qilgan barcha xodimlar.
- `topkey_get_user_attendance` — bitta xodimning davomat tarixi (`user_id` + `year`/`month` yoki `from_date`/`to_date`).
- `topkey_get_team_summary` — kunlik sarhisob: `present`, `absent`, `late`, `on_leave`.
- `topkey_get_late_arrivals` — kech kelgan xodimlar (`date`).
- `topkey_get_overtime` — sverxurochniy hisobot (`user_id`, `from_date`, `to_date`).

## HR — Ta'tillar (5)
- `topkey_list_leave_requests` — ta'til so'rovlari. Filter: `status` (pending|approved|rejected), `user_id`, `from_date`/`to_date`.
- `topkey_create_leave_request` — admin ta'til berish (`user_id`, `leave_type_id`, `start_date`, `end_date`, ixtiyoriy `reason`).
- `topkey_approve_leave` — ta'tilni tasdiqlash (`leave_id`).
- `topkey_get_leave_balance` — xodimning qolgan ta'til kunlari (`user_id`).
- `topkey_list_leave_types` — ta'til turlari.

## Loyihalar (3)
- `topkey_list_projects` — loyihalar ro'yxati. Filter: `status`, `client_id`, `category_id`. `all=true` — avto-paginatsiya (max 5 sahifa / 500 ta).
- `topkey_get_project` — loyiha tafsilotlari (`project_id`).
- `topkey_list_project_members` — loyiha a'zolari (`project_id`).

## Vazifalar (5)
- `topkey_list_tasks` — vazifalar. Filter: `project_id`, `assigned_to`, `status`, `board_column_id`.
- `topkey_get_task` — bitta vazifa (`task_id`).
- `topkey_create_task` — yangi vazifa (`title`, `project_id`; ixtiyoriy `description`, `assigned_to`, `due_date`, `priority`).
- `topkey_update_task_status` — status o'zgartirish (`task_id` + `status` YOKI `board_column_id`).
- `topkey_list_subtasks` — sub-vazifalar (`task_id`).

## Vaqt qaydlari (2)
- `topkey_log_time` — vazifaga vaqt qayd qilish (`task_id`, `hours`; ixtiyoriy `date`, `memo`).
- `topkey_list_my_timelogs` — joriy foydalanuvchi qaydlari (`from_date`, `to_date`).

## Auth (3)
- `topkey_login` — token qayta olish. ODATDA KERAK EMAS (avto re-login 401 da).
- `topkey_get_current_user` — bot qaysi foydalanuvchi sifatida ishlayotganini ko'rsatadi.
- `topkey_list_users` — tizim foydalanuvchilari (admin only).

## Qoidalar
- Sana formati: `YYYY-MM-DD`.
- `topkey_login` ni o'zingiz chaqirmang — avtomatik bajariladi.
- Foydalanuvchiga \"API\", \"endpoint\", \"token\" kabi texnik atamalarni ishlatmang. Oddiy javob bering: \"Hisobotni tayyorladim\", \"Ta'til so'rovini tasdiqladim\".
- Javoblarni Uzbek tilida yozing.
- Katta ro'yxat (loyihalar) uchun `all=true` ni ishlating — 1 chaqiruvda barcha sahifalar olinadi.
