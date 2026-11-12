-- Пункты плана лотов (цена за единицу, единица, ЕНС ТРУ, аванс) OWS отдаёт с задержкой после
-- публикации: у объявлений, прочитанных в первые часы, их нет почти у половины лотов.
-- plans_missing — у какого-то лота пунктов нет: такие подробности перечитываем чаще (details.refresh).
ALTER TABLE announcements ADD COLUMN IF NOT EXISTS plans_missing boolean NOT NULL DEFAULT false;
-- Открытые сейчас — сразу в очередь; у прочих флаг выставит следующее чтение.
UPDATE announcements SET plans_missing = true
WHERE end_date > now() AND system = 1 AND detail IS NOT NULL
  -- Как details._no_plans: лот не удалён и пунктов нет (пусто, null или поля нет).
  AND jsonb_path_exists(detail, '$.Lots[*] ? (!exists(@.isDeleted ? (@ == 1 || @ == true))
                                              && !exists(@.Plans[*] ? (@.type() == "object")))');
