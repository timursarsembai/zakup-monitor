-- Сессия «вход как пользователь»: кто из администраторов её открыл. Действия в ней
-- пишутся в журнал с пометкой администратора (zakup.web.core._audit).
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS impersonated_by bigint REFERENCES users (id) ON DELETE CASCADE;
