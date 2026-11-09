-- Узкие строки: сырой ответ портала (raw) и длинные тексты — в отдельное хранилище (TOAST),
-- в строке остаётся то, по чему ищут и что показывают в списках. Просмотр таблиц lots и
-- announcements читает вдвое меньше страниц. Меняет правила для новых записей; старые
-- строки переписываются отдельно (UPDATE … SET raw = raw, затем VACUUM FULL — см. README).
ALTER TABLE lots SET (toast_tuple_target = 512);
ALTER TABLE lots ALTER COLUMN tsv SET STORAGE MAIN, ALTER COLUMN tsv_kk SET STORAGE MAIN,
                 ALTER COLUMN name_ru SET STORAGE MAIN, ALTER COLUMN name_kz SET STORAGE MAIN,
                 ALTER COLUMN customer_name_ru SET STORAGE MAIN;
ALTER TABLE announcements SET (toast_tuple_target = 512);
ALTER TABLE announcements ALTER COLUMN tsv SET STORAGE MAIN, ALTER COLUMN tsv_kk SET STORAGE MAIN,
                          ALTER COLUMN name_ru SET STORAGE MAIN, ALTER COLUMN name_kz SET STORAGE MAIN,
                          ALTER COLUMN customer_name_ru SET STORAGE MAIN, ALTER COLUMN org_name_ru SET STORAGE MAIN;
