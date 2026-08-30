import { useI18n } from '../i18n'

interface Quote {
  quote: string
  source: string
}

interface Solution {
  name: string
  strengths: string
  weaknesses: string
}

interface Props {
  data: Record<string, unknown>
}

export default function AnalysisCard({ data }: Props) {
  const { text } = useI18n()
  const scores = (data.femwc_scores || {}) as Record<string, number>
  const quotes = (data.verbatim_quotes || []) as Quote[]
  const solutions = (data.existing_solutions || []) as Solution[]
  const dims: [string, string][] = [
    ['F', text('analysis.frequency')],
    ['E', text('analysis.emotion')],
    ['M', text('analysis.market')],
    ['W', text('analysis.payment')],
    ['C', text('analysis.competitor')],
  ]

  return (
    <div className="space-y-4">
      {data.pain_point != null && (
        <p className="text-sm">
          <span className="font-semibold">{text('analysis.corePain')}</span>
          {String(data.pain_point)}
        </p>
      )}
      {data.jtbd != null && (
        <p className="text-sm">
          <span className="font-semibold">JTBD：</span>
          {String(data.jtbd)}
        </p>
      )}

      {Object.keys(scores).length > 0 && (
        <div>
          <div className="grid grid-cols-5 max-md:grid-cols-3 gap-2">
            {dims.map(([key, label]) => (
              <div
                key={key}
                className="bg-white rounded-xl p-3 text-center border border-border"
              >
                <p className="text-[10px] text-muted font-medium">
                  {key} {label}
                </p>
                <p className="text-lg font-bold mt-0.5">{scores[key] ?? 0}/5</p>
              </div>
            ))}
          </div>
          {scores.total !== undefined && (
            <p className="text-sm font-semibold mt-2">
              {text('analysis.opportunityScore')}{scores.total}/5.00
            </p>
          )}
        </div>
      )}

      {quotes.length > 0 && (
        <div>
          <p className="text-sm font-semibold mb-1.5">{text('analysis.userQuotes')}</p>
          <div className="space-y-1.5">
            {quotes.slice(0, 5).map((q, i) => (
              <div key={i}>
                <blockquote className="text-sm italic border-l-3 border-signal pl-3 text-text/80">
                  &quot;{q.quote}&quot;
                </blockquote>
                <p className="text-[10px] text-muted mt-0.5 ml-3">— {q.source}</p>
              </div>
            ))}
          </div>
        </div>
      )}

      {data.target_users != null && (
        <p className="text-sm">
          <span className="font-semibold">{text('analysis.targetUsers')}</span>
          {String(data.target_users)}
        </p>
      )}
      {data.opportunity != null && (
        <p className="text-sm">
          <span className="font-semibold">{text('analysis.productOpportunity')}</span>
          {String(data.opportunity)}
        </p>
      )}

      {solutions.length > 0 && (
        <div>
          <p className="text-sm font-semibold mb-1">{text('analysis.existingSolutions')}</p>
          {solutions.map((s, i) => (
            <p key={i} className="text-xs text-muted">
              <span className="font-medium text-text">{s.name}</span>：{s.strengths}{' '}
              / <em>{s.weaknesses}</em>
            </p>
          ))}
        </div>
      )}

      {data.confidence !== undefined && (
        <p className="text-xs text-muted">
          {text('analysis.confidence')}{String(data.confidence)}/10 — {String(data.reasoning || '').slice(0, 200)}
        </p>
      )}
    </div>
  )
}
