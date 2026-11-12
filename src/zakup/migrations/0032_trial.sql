-- Пробный месяц: 30 дней платных функций после регистрации (поиск товаров с ИИ — по-прежнему
-- за деньги, он считается балансом поисков). Срок пробы — отдельно от оплаченного, чтобы
-- аналитика и возвраты видели только настоящие оплаты. Доступ — пока не истёк любой из двух.
ALTER TABLE users ADD COLUMN IF NOT EXISTS trial_until timestamptz;

-- Клиентам, которые ещё ни разу не платили, проба начинается сейчас.
UPDATE users SET trial_until = now() + interval '30 days'
 WHERE role = 'user' AND deleted_at IS NULL AND paid_until IS NULL AND trial_until IS NULL;
