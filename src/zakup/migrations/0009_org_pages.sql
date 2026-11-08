-- Страницы организаций (zakup.orgs): закупки организатора, победы и участие поставщика.
CREATE INDEX IF NOT EXISTS announcements_org_bin ON announcements (org_bin);
CREATE INDEX IF NOT EXISTS lot_results_winner_bin ON lot_results (winner_bin);
CREATE INDEX IF NOT EXISTS lot_results_offers ON lot_results USING gin (offers jsonb_path_ops);
