-- 保留已发放记录依赖的历史规则，同时停用服务端已撤下的规则。
ALTER TABLE breadcrumb_rules
ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1));
