-- Массивы, по которым фильтруют (коды ЕНС ТРУ, места поставки), — в строке, а не в TOAST
-- (0013 вынесла их вместе с raw: фильтр по категории распаковывал их у каждой строки).
ALTER TABLE lots ALTER COLUMN enstru SET STORAGE MAIN, ALTER COLUMN kato SET STORAGE MAIN;
ALTER TABLE announcements ALTER COLUMN kato SET STORAGE MAIN;
