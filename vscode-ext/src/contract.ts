export type MeasurementStatus = "complete" | "incomplete" | "error";
export type ReportKind = "run" | "diff" | "range" | "bisect";

export interface Commit {
  sha: string;
  short: string;
  author: string;
  subject: string;
  date?: string;
}

export interface Stats {
  median: number;
  min: number;
  max: number;
  samples?: number[];
}

export interface FunctionDelta {
  id: string;
  file?: string;
  qualname?: string;
  line?: number;
  cum_delta: number;
  self_delta: number;
  changed?: boolean;
}

export interface SourceLine {
  file: string;
  line: number;
  bytes: number;
}

export interface Hunk {
  file: string;
  header: string;
  new_start: number;
}

export interface Verdict {
  kind: "none" | "direct" | "indirect" | "unattributed";
  function?: string;
  file?: string;
  qualname?: string;
  line?: number;
  cum_delta?: number;
  self_delta?: number;
  reason?: string;
  note?: string;
  hunks?: Hunk[];
  hot_lines?: SourceLine[];
  allocated_at?: SourceLine[];
}

export interface Finding {
  unit: string;
  metric: "peak" | "retained";
  delta: number;
  base: number;
  head: number;
  band: number;
  verdict: Verdict;
  functions: FunctionDelta[];
  commit?: string;
  parent?: string;
}

export interface UnitResult {
  outcome: string;
  peak: Stats;
  end: Stats;
  top?: FunctionDelta[];
}

export interface ComparisonMetric {
  metric: "peak" | "retained";
  base: number;
  head: number;
  delta: number;
  band: number;
  significant: boolean;
}

export interface ComparisonUnit {
  name: string;
  status: "compared" | "new" | "removed" | "outcome_changed";
  outcome?: { base: string; head: string };
  metrics?: ComparisonMetric[];
}

export interface Point {
  commit: Commit;
  measured: boolean;
  valid: boolean;
  units: Record<string, UnitResult>;
}

export interface MeasurementResult {
  valid: boolean;
  runs: number;
  units: Record<string, UnitResult>;
}

export interface BisectMeasurement {
  commit: Commit;
  value: number | null;
  bad: boolean;
  skipped?: boolean;
}

export interface MemblameResult {
  schema: 1;
  kind: ReportKind;
  repo: string;
  workload: string;
  python: string;
  settings: { runs: number; nframe: number; timeout: number; pythonpath: string[] | null };
  measurement_status: MeasurementStatus;
  warnings: string[];
  notes?: string[];
  findings?: Finding[];
  commit?: Commit;
  base?: Commit;
  head?: Commit;
  result?: MeasurementResult;
  valid?: boolean;
  units?: ComparisonUnit[];
  points?: Point[];
  mode?: "adaptive" | "exhaustive";
  measured?: number;
  incomplete_commits?: number;
  status?: "found" | "no_regression" | "error";
  message?: string;
  culprit?: Commit;
  parent?: Commit;
  threshold?: number;
  steps?: number;
  candidates?: number;
  metric?: "peak" | "retained";
  unit?: string;
  verified?: boolean;
  monotonic?: boolean | null;
  measurements?: BisectMeasurement[];
  changed_functions?: Array<{ id: string; file: string; line: number }>;
}

export interface EngineError {
  schema: 1;
  kind: "error";
  error: string;
}

function object(value: unknown): Record<string, unknown> | undefined {
  return typeof value === "object" && value !== null ? value as Record<string, unknown> : undefined;
}

export function parseEngineResponse(value: unknown): MemblameResult | EngineError {
  const data = object(value);
  if (!data) {
    throw new Error("memblame returned a non-object result");
  }
  if (data.schema !== 1) {
    throw new Error(`unsupported memblame result schema ${String(data.schema)}; expected 1`);
  }
  if (data.kind === "error") {
    if (typeof data.error !== "string") throw new Error("memblame error result has no message");
    return data as unknown as EngineError;
  }
  if (!["run", "diff", "range", "bisect"].includes(String(data.kind))) {
    throw new Error(`invalid memblame result kind ${String(data.kind)}`);
  }
  for (const key of ["repo", "workload", "python"] as const) {
    if (typeof data[key] !== "string") throw new Error(`memblame result field ${key} is missing`);
  }
  if (!["complete", "incomplete", "error"].includes(String(data.measurement_status))) {
    throw new Error(`invalid memblame measurement status ${String(data.measurement_status)}`);
  }
  return data as unknown as MemblameResult;
}
