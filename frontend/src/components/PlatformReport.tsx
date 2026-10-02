import { useEffect, useState } from "react";
import type { ReactNode } from "react";

import { getPlatformReport } from "../api";
import type {
  PlatformClaim,
  PlatformReport as PlatformReportT,
  PlatformSection,
  PlatformTable,
} from "../types";
import { Panel } from "./ui";

/** A path in the repository, linked to its page on GitHub. */
function Source({ repository, path }: { repository: string; path: string }): ReactNode {
  return (
    <a
      href={`${repository}/blob/main/${path}`}
      className="font-mono text-xs text-sky-400 hover:text-sky-300"
      target="_blank"
      rel="noreferrer"
    >
      {path}
    </a>
  );
}

function Sources({
  repository,
  sources,
  doc,
}: {
  repository: string;
  sources: string[];
  doc: string;
}): ReactNode {
  return (
    <p className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-slate-500">
      <span>records:</span>
      {sources.map((path) => (
        <Source key={path} repository={repository} path={path} />
      ))}
      <span>write-up:</span>
      <Source repository={repository} path={doc} />
    </p>
  );
}

function Claim({ claim, repository }: { claim: PlatformClaim; repository: string }): ReactNode {
  return (
    <Panel title={claim.claim}>
      <p className="text-lg font-semibold text-slate-100">{claim.value}</p>
      <p className="mt-2 text-sm text-slate-300">{claim.detail}</p>
      <p className="mt-2 text-xs text-slate-500">
        <span className="font-semibold text-slate-400">measured on: </span>
        {claim.config}
      </p>
      <Sources repository={repository} sources={claim.sources} doc={claim.doc} />
    </Panel>
  );
}

function Table({ table }: { table: PlatformTable }): ReactNode {
  return (
    <div className="mt-4 overflow-x-auto">
      <h4 className="mb-2 text-sm font-medium text-slate-300">{table.title}</h4>
      <table className="w-full text-left text-sm">
        <thead>
          <tr className="border-b border-slate-800 text-xs uppercase tracking-wide text-slate-500">
            {table.columns.map((column) => (
              <th key={column} className="px-2 py-1.5 font-medium">
                {column}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {table.rows.map((row) => (
            <tr key={row.join("|")} className="border-b border-slate-800/60 text-slate-300">
              {row.map((cell, index) => (
                <td key={`${table.columns[index]}`} className="whitespace-nowrap px-2 py-1.5">
                  {cell}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Section({
  section,
  repository,
}: {
  section: PlatformSection;
  repository: string;
}): ReactNode {
  return (
    <Panel title={section.title}>
      <p className="text-xs text-slate-500">
        <span className="font-semibold text-slate-400">measured on: </span>
        {section.config}
      </p>
      <ul className="mt-3 list-disc space-y-1 pl-5 text-sm text-slate-300">
        {section.points.map((point) => (
          <li key={point}>{point}</li>
        ))}
      </ul>
      {section.tables.map((table) => (
        <Table key={table.title} table={table} />
      ))}
      <Sources repository={repository} sources={section.sources} doc={section.doc} />
    </Panel>
  );
}

/** Every measured result of the execution platform, rendered from the committed records. */
export function PlatformReport(): ReactNode {
  const [report, setReport] = useState<PlatformReportT | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    getPlatformReport()
      .then((data) => active && setReport(data))
      .catch((e: unknown) => active && setError(e instanceof Error ? e.message : String(e)));
    return () => {
      active = false;
    };
  }, []);

  if (error) {
    return (
      <p className="rounded-lg border border-rose-500/30 bg-rose-500/10 px-4 py-3 text-sm text-rose-300">
        could not load the platform report: {error}
      </p>
    );
  }
  if (!report) {
    return <p className="text-sm text-slate-500">loading the platform report…</p>;
  }
  return (
    <div className="flex flex-col gap-4">
      <p className="text-sm text-slate-400">{report.about}</p>
      {report.headline.map((claim) => (
        <Claim key={claim.id} claim={claim} repository={report.repository} />
      ))}
      {report.sections.map((section) => (
        <Section key={section.id} section={section} repository={report.repository} />
      ))}
    </div>
  );
}
