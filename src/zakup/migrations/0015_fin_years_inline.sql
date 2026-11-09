-- Год финансирования — фильтр «Финансовый год»: в строке, а не в TOAST (как kato/enstru в 0014).
ALTER TABLE announcements ALTER COLUMN fin_years SET STORAGE MAIN;
