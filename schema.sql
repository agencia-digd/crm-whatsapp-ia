-- Modelo mínimo (PostgreSQL). Tudo com tenant_id para isolar clientes.
CREATE TABLE users (id bigserial PRIMARY KEY, tenant_id int NOT NULL, nome text NOT NULL, telefone text, papel text NOT NULL DEFAULT 'vendedor', ativo boolean NOT NULL DEFAULT true);
CREATE TABLE pipeline_stages (id bigserial PRIMARY KEY, tenant_id int NOT NULL, nome text NOT NULL, ordem int NOT NULL, tipo text);
CREATE TABLE leads (id bigserial PRIMARY KEY, tenant_id int NOT NULL, external_ref text, nome text, telefone text, owner_id bigint REFERENCES users(id), stage_id bigint REFERENCES pipeline_stages(id), origem text, valor numeric, created_at timestamptz DEFAULT now(), updated_at timestamptz DEFAULT now());
CREATE INDEX ON leads (tenant_id, telefone);
CREATE TABLE lead_events (id bigserial PRIMARY KEY, tenant_id int NOT NULL, lead_id bigint REFERENCES leads(id), tipo text, payload jsonb, at timestamptz DEFAULT now());
CREATE TABLE mensagens (id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id int NOT NULL, lead_id bigint, telefone text, from_vendedor boolean, tipo text, texto text, metadata jsonb, ts timestamptz DEFAULT now());
CREATE TABLE conversa_lida (usuario text, telefone text, lido_ate timestamptz, PRIMARY KEY (usuario, telefone));
CREATE TABLE tarefas (id bigserial PRIMARY KEY, tenant_id int NOT NULL, lead_id bigint, owner_id bigint, data_tarefa date, texto text, status text DEFAULT 'aberta', criado_em timestamptz DEFAULT now(), concluido_em timestamptz);
CREATE TABLE visitas (id bigserial PRIMARY KEY, tenant_id int NOT NULL, lead_id bigint, owner_id bigint, data_hora timestamptz, status text, local text);
CREATE TABLE vendas (id bigserial PRIMARY KEY, tenant_id int NOT NULL, lead_id bigint, owner_id bigint, valor numeric, data_venda date, origem text);
-- Régua
CREATE TABLE cadencia_copies (id bigserial PRIMARY KEY, tenant_id int NOT NULL, toque int NOT NULL, texto text, atualizado_em timestamptz DEFAULT now());
CREATE TABLE cadencia_estado (id bigserial PRIMARY KEY, tenant_id int NOT NULL, lead_id bigint, telefone text, toque_atual int, proximo_toque_em timestamptz, status text, atualizado_em timestamptz DEFAULT now());
CREATE TABLE cadencia_eventos (id bigserial PRIMARY KEY, tenant_id int NOT NULL, cadencia_id bigint REFERENCES cadencia_estado(id), toque int, tipo text, canal text, payload jsonb, at timestamptz DEFAULT now());
CREATE TABLE opt_out (tenant_id int NOT NULL, telefone text NOT NULL, motivo text, frase text, em timestamptz DEFAULT now(), PRIMARY KEY (tenant_id, telefone));
-- Ligações
CREATE TABLE ligacao_reserva (id bigserial PRIMARY KEY, tenant_id int NOT NULL, lead_id bigint, dono text, reservado_em timestamptz DEFAULT now(), expira_em timestamptz);
CREATE TABLE ligacao_resultado (id bigserial PRIMARY KEY, tenant_id int NOT NULL, lead_id bigint, atendeu boolean, quis_continuar boolean, observacao text, autor text, criado_em timestamptz DEFAULT now());
-- IA
CREATE TABLE shadow_decisions (id bigserial PRIMARY KEY, tenant_id int NOT NULL, lead_id bigint, resposta_sugerida text, motivo text, confianca int, modelo text, acerto boolean, avaliado_por text, juiz_veredito text, juiz_motivo text, criado_em timestamptz DEFAULT now());
CREATE TABLE llm_chamadas (id bigserial PRIMARY KEY, criado_em timestamptz DEFAULT now(), servico text, provider text, modelo text, input_tokens int, output_tokens int, duracao_ms int, erro text, custo_usd numeric(12,6));
-- Config
CREATE TABLE config_operacional (chave text PRIMARY KEY, valor jsonb, atualizado_em timestamptz DEFAULT now(), atualizado_por text);
CREATE TABLE config_operacional_historico (id bigserial PRIMARY KEY, chave text, acao text, valor_antes jsonb, valor_depois jsonb, autor text, em timestamptz DEFAULT now());
