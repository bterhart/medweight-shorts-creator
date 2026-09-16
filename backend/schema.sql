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
