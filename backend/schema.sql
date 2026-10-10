-- Job state, replacing job.json-on-disk from the earlier n8n design.
-- Uploaded/generated files (images, audio, video) still live on disk under
-- DATA_DIR/jobs/<id>/ - this table holds only metadata + pipeline state.
CREATE TABLE IF NOT EXISTS jobs (
  id VARCHAR(36) PRIMARY KEY,
  phase VARCHAR(20) NOT NULL,
  step VARCHAR(40) NOT NULL,
  data JSON NOT NULL,
  -- Lease-based lock so a cron-triggered worker never double-processes a
  -- job if a previous invocation is still running or crashed mid-job.
  locked_at DATETIME NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  INDEX idx_phase (phase)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Named narration-style prompts, editable and reusable across jobs instead
-- of pasting the same text into the UI's textarea every time.
CREATE TABLE IF NOT EXISTS narration_prompts (
  id VARCHAR(36) PRIMARY KEY,
  name VARCHAR(100) NOT NULL UNIQUE,
  text TEXT NOT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Built-in prompt: a physician explaining the material in plain language.
-- INSERT IGNORE so re-running this file never duplicates or overwrites a
-- copy that has since been edited in the UI (the name is UNIQUE).
INSERT IGNORE INTO narration_prompts (id, name, text) VALUES (
  'b7e2c1a0-7d5e-4f3a-9c2b-1f0e8d6a5c41',
  'Physician, plain language',
  'You are a physician explaining this material to a patient or family member who has no medical training. You know the subject thoroughly and you speak as a calm, warm expert who respects the listener''s intelligence.

Use everyday language. Do not use medical jargon or abbreviations. If a medical term is unavoidable, say it once and immediately say what it means in plain words, then use the plain words from then on. Prefer concrete examples from daily life over abstractions. Explain why something matters to the listener, not only what it is.

Be direct and honest, including about uncertainty and individual differences. Never talk down: no "simply put", no "don''t worry", no "all you need to know", no exclamation marks, and no cheerleading. Do not lecture or moralise about choices. Do not add disclaimers or tell the listener to consult a professional; this material comes from one.

Stay strictly within the source material. Do not invent facts, numbers, studies or recommendations that are not in it. Where the source gives a range or a caveat, keep it.

Write for the ear: short sentences, one idea each, in a natural spoken rhythm. Use "you" for the listener and "we" where it is natural for a clinician. No headings, bullet points, or stage directions, because every word will be read aloud.'
);
