import { useCallback, useEffect, useMemo, useState } from "react"
import {
  Activity,
  Apple,
  ArrowRight,
  Check,
  ChevronDown,
  ChevronRight,
  Dumbbell,
  HeartPulse,
  LoaderCircle,
  Moon,
  RefreshCw,
  Route,
  Zap,
} from "lucide-react"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Progress } from "@/components/ui/progress"
import { Skeleton } from "@/components/ui/skeleton"
import { Switch } from "@/components/ui/switch"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip"

type TodayPayload = {
  ok?: boolean
  today?: {
    session_label?: string
    session_type?: string
  }
  load?: {
    has_data?: boolean
    momentum_pct?: number | null
  }
  last_logs?: {
    nutrition?: { date_iso?: string }
    runs?: { date?: string }
    training?: { date_iso?: string; name?: string }
  }
}

type SignalItem = {
  state?: string
  value?: number | null
  recoveryScore?: number | null
  kcal_intake?: number | null
}

type SignalsPayload = {
  ok?: boolean
  days?: string[]
  lanes?: {
    recovery?: SignalItem[]
    energy?: SignalItem[]
    run_load?: SignalItem[]
    strength?: SignalItem[]
  }
  context?: {
    recovery?: { today?: number | null; avg_7d?: number | null; state?: string; series?: Array<number | null> }
    energy?: { avg_3d?: number | null; state?: string; series?: Array<number | null> }
    load?: { series?: Array<number | null> }
  }
}

type AgendaItem = {
  time_label?: string
  title?: string
  subtitle?: string
  kind?: string
  state?: string
  source_label?: string
}

type AgendaPayload = {
  ok?: boolean
  day?: string
  items?: AgendaItem[]
}

type TrainingExercise = {
  name?: string
  planned_sets?: number | null
  target_rep_range?: string
  equipment?: string
  notes?: string
}

type TrainingPayload = {
  ok?: boolean
  title?: string
  headline?: string
  subtitle?: string
  status?: string
  global_note?: string | null
  items?: Array<{ exercise?: string; badge?: string; middle_note?: string; left?: string; action?: string; muted?: boolean }>
  fallback?: { visible?: boolean; message?: string }
  partial?: boolean
  refresh_triggered?: boolean
  planned_session?: {
    available?: boolean
    name?: string
    weekday?: string
    time?: string | null
    exercises?: TrainingExercise[]
  }
}

type Meal = {
  slot_id?: number
  time_text?: string
  title?: string
  status?: string
  logged_meal_id?: number | null
  items?: Array<{ food_name?: string; amount?: number; unit?: string }>
  macros?: { kcal?: number; p?: number; c?: number; f?: number }
}

type NutritionPayload = {
  ok?: boolean
  date?: string
  planned_meals?: Meal[]
  planned_totals?: { kcal?: number; p?: number; c?: number; f?: number }
  logged_totals?: { kcal?: number; p?: number; c?: number; f?: number }
  targets?: { kcal?: number; p?: number; c?: number; f?: number; template_title?: string }
}

type EndurancePayload = {
  ok?: boolean
  source?: string
  requested_day?: string | null
  plan?: { id?: string; title?: string } | null
  phase?: { id?: string; name?: string; phase_type?: string } | null
  session?: {
    id?: string
    title?: string
    scheduled_date?: string
    session_type?: string
    duration_s?: number | null
    distance_m?: number | null
    notes?: string | null
    execution_steps?: Array<{
      id?: string
      execution_index?: number
      kind?: string
      duration_s?: number | null
      distance_m?: number | null
      repeat_group_id?: string | null
      repeat_index?: number | null
      repeat_count?: number | null
      repeat_label?: string | null
    }>
  } | null
  sync?: {
    state?: string
    intervals_synced?: boolean
    garmin_state?: string
    label?: string
    last_synced_at?: string | null
    error?: string | null
  } | null
}

type BlockPayload = {
  ok?: boolean
  badge?: string
  status_line?: string
  plan_name?: string
  block_week?: number | null
  block_weeks_total?: number | null
}

type DashboardData = {
  today: TodayPayload | null
  signals: SignalsPayload | null
  agenda: AgendaPayload | null
  training: TrainingPayload | null
  nutrition: NutritionPayload | null
  endurance: EndurancePayload | null
  block: BlockPayload | null
}

const EMPTY_DATA: DashboardData = {
  today: null,
  signals: null,
  agenda: null,
  training: null,
  nutrition: null,
  endurance: null,
  block: null,
}

type DashboardLoading = Record<keyof DashboardData, boolean>

const AGENDA_MEALS_STORAGE_KEY = "liva_dashboard_agenda_meals_visible"

const INITIAL_LOADING: DashboardLoading = {
  today: true,
  signals: true,
  agenda: true,
  training: true,
  nutrition: true,
  endurance: true,
  block: true,
}

function berlinDayIso() {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "Europe/Berlin",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date())
}

function formatLongDate(dateIso = berlinDayIso()) {
  const date = new Date(`${dateIso}T12:00:00Z`)
  return new Intl.DateTimeFormat("de-DE", {
    timeZone: "Europe/Berlin",
    weekday: "long",
    day: "2-digit",
    month: "long",
  }).format(date)
}

function weekdayLabel(dateIso: string) {
  const date = new Date(`${dateIso}T12:00:00Z`)
  return new Intl.DateTimeFormat("de-DE", {
    timeZone: "Europe/Berlin",
    weekday: "short",
  }).format(date).replace(".", "")
}

function number(value: unknown, fallback = 0) {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : fallback
}

function round(value: unknown) {
  return Math.round(number(value))
}

function formatSignedPercent(value: unknown) {
  const parsed = number(value)
  return `${parsed > 0 ? "+" : ""}${Math.round(parsed)} %`
}

function formatRunDate(dateIso?: string) {
  if (!dateIso) return "Nächster Termin offen"
  const date = new Date(`${dateIso}T12:00:00Z`)
  return new Intl.DateTimeFormat("de-DE", {
    timeZone: "Europe/Berlin",
    weekday: "short",
    day: "2-digit",
    month: "2-digit",
  }).format(date).replace(",", " ·")
}

function formatRunDuration(durationSeconds?: number | null) {
  if (!durationSeconds) return "—"
  const minutes = Math.round(durationSeconds / 60)
  return `${minutes} min`
}

function formatStepDuration(durationSeconds?: number | null) {
  if (!durationSeconds) return "offen"
  if (durationSeconds < 60) return `${durationSeconds} s`
  const minutes = Math.floor(durationSeconds / 60)
  const seconds = durationSeconds % 60
  return seconds ? `${minutes}:${String(seconds).padStart(2, "0")} min` : `${minutes} min`
}

function formatRunDistance(distanceMeters?: number | null) {
  if (!distanceMeters) return "—"
  return `${(distanceMeters / 1000).toLocaleString("de-DE", { maximumFractionDigits: 1 })} km`
}

const runStepTone: Record<string, string> = {
  warmup: "bg-sky-400/55",
  open: "bg-emerald-400/55",
  work: "bg-blue-400/90",
  interval: "bg-blue-400/90",
  stride: "bg-cyan-300/90",
  recovery: "bg-slate-400/45",
  cooldown: "bg-teal-400/55",
}

const runTypeLabel: Record<string, string> = {
  easy: "Locker",
  long: "Lang",
  threshold: "Schwelle",
  intervals: "Intervalle",
  interval: "Intervalle",
  race: "Wettkampf",
  recovery: "Regeneration",
}

async function getJson<T>(url: string, signal: AbortSignal, timeoutMs = 10_000): Promise<T> {
  const requestSignal = AbortSignal.any([signal, AbortSignal.timeout(timeoutMs)])
  const response = await fetch(url, { cache: "no-store", signal: requestSignal })
  if (!response.ok) throw new Error(`${url}: ${response.status}`)
  return response.json() as Promise<T>
}

function trainingCardIsPending(payload: TrainingPayload | null) {
  return payload?.status === "loading"
}

function waitForRetry(delayMs: number, signal: AbortSignal) {
  return new Promise<void>((resolve, reject) => {
    const timeout = window.setTimeout(resolve, delayMs)
    signal.addEventListener("abort", () => {
      window.clearTimeout(timeout)
      reject(new DOMException("Aborted", "AbortError"))
    }, { once: true })
  })
}

async function pollPendingTrainingCard(url: string, signal: AbortSignal) {
  let payload: TrainingPayload | null = null
  for (let attempt = 0; attempt < 12; attempt += 1) {
    await waitForRetry(Math.min(300 + attempt * 250, 1_500), signal)
    payload = await getJson<TrainingPayload>(url, signal)
    if (!trainingCardIsPending(payload)) return payload
  }
  return {
    ...payload,
    status: "failed",
    fallback: {
      visible: true,
      message: "Training-Card konnte nicht rechtzeitig geladen werden. Bitte Dashboard aktualisieren.",
    },
  }
}

function useDashboardData() {
  const [data, setData] = useState<DashboardData>(EMPTY_DATA)
  const [loading, setLoading] = useState<DashboardLoading>({ ...INITIAL_LOADING })
  const [errors, setErrors] = useState<string[]>([])
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null)
  const [revision, setRevision] = useState(0)

  const refresh = useCallback(() => setRevision((value) => value + 1), [])

  useEffect(() => {
    const controller = new AbortController()
    const day = berlinDayIso()
    const requests = {
      today: `/api/dashboard/today?day=${day}`,
      signals: "/api/dashboard/signals?days=14",
      agenda: `/api/dashboard/today_agenda?day=${day}`,
      training: `/api/dashboard/training_today_card?date=${day}`,
      nutrition: `/api/nutrition/logging/day?date=${day}`,
      endurance: `/api/dashboard/endurance-approval?day=${day}`,
      block: "/api/dashboard/block_status",
    } as const

    setLoading({ ...INITIAL_LOADING })
    setErrors([])

    Object.entries(requests).forEach(([rawKey, url]) => {
      const key = rawKey as keyof DashboardData
      void (async () => {
        try {
          let payload = await getJson<DashboardData[typeof key]>(url, controller.signal)
          if (controller.signal.aborted) return
          setData((current) => ({ ...current, [key]: payload }))
          setUpdatedAt(new Date())

          if (key === "training" && trainingCardIsPending(payload as TrainingPayload | null)) {
            payload = await pollPendingTrainingCard(url, controller.signal) as DashboardData[typeof key]
            if (controller.signal.aborted) return
            setData((current) => ({ ...current, training: payload as TrainingPayload }))
            setUpdatedAt(new Date())
          }
        } catch (error) {
          if (controller.signal.aborted || error instanceof DOMException && error.name === "AbortError") return
          const message = error instanceof Error ? error.message : String(error || "Unbekannter Fehler")
          setErrors((current) => [...current, `${key}: ${message}`])
        } finally {
          if (!controller.signal.aborted) {
            setLoading((current) => ({ ...current, [key]: false }))
          }
        }
      })()
    })

    return () => controller.abort()
  }, [revision])

  return { data, loading, errors, updatedAt, refresh }
}

type CardTone = "today" | "recovery" | "nutrition" | "training" | "endurance" | "neutral"

const cardToneClasses: Record<CardTone, string> = {
  today: "bg-card",
  recovery: "bg-card",
  nutrition: "bg-card",
  training: "bg-card",
  endurance: "bg-card",
  neutral: "bg-card",
}

function AppCard({ className = "", tone = "neutral", ...props }: React.ComponentProps<typeof Card> & { tone?: CardTone }) {
  return <Card className={`border-border shadow-[var(--card-shadow)] ${cardToneClasses[tone]} ${className}`} {...props} />
}

const textToneClasses: Record<CardTone, string> = {
  today: "text-primary",
  recovery: "text-[var(--domain-recovery)]",
  nutrition: "text-[var(--domain-nutrition)]",
  training: "text-[var(--domain-training)]",
  endurance: "text-[var(--domain-endurance)]",
  neutral: "text-muted-foreground",
}

function CardHeading({ eyebrow, title, meta, action, tone = "neutral" }: { eyebrow: string; title: string; meta?: string; action?: React.ReactNode; tone?: CardTone }) {
  return (
    <CardHeader className="gap-x-4">
      <div className="min-w-0 space-y-1">
        <CardDescription className={`text-[0.65rem] font-semibold uppercase tracking-[0.16em] ${textToneClasses[tone]}`}>{eyebrow}</CardDescription>
        <CardTitle className="text-base font-semibold tracking-tight sm:text-lg">{title}</CardTitle>
      </div>
      {action || meta ? (
        <CardAction>
          {action || <span className="shrink-0 text-[0.68rem] text-muted-foreground">{meta}</span>}
        </CardAction>
      ) : null}
    </CardHeader>
  )
}

function CardLoading({ lines = 3 }: { lines?: number }) {
  return (
    <div className="space-y-3 px-4 pb-2">
      {Array.from({ length: lines }, (_, index) => (
        <Skeleton className={`h-10 ${index === lines - 1 ? "w-4/5" : "w-full"}`} key={index} />
      ))}
    </div>
  )
}

function Sparkline({ series, color, id }: { series: Array<number | null | undefined>; color: string; id: string }) {
  const width = 240
  const height = 76
  const padding = 5
  const values = series.filter((value): value is number => value != null && Number.isFinite(value))
  if (!values.length) return <div className="grid h-[4.75rem] place-items-center text-[0.68rem] text-muted-foreground">Noch kein Verlauf</div>
  const min = Math.min(...values)
  const max = Math.max(...values)
  const spread = Math.max(max - min, 1)
  const point = (value: number, index: number) => ({
    x: padding + (index / Math.max(series.length - 1, 1)) * (width - padding * 2),
    y: padding + ((max - value) / spread) * (height - padding * 2),
  })
  const segments: Array<Array<{ x: number; y: number }>> = []
  let active: Array<{ x: number; y: number }> = []
  series.forEach((value, index) => {
    if (value == null || !Number.isFinite(value)) {
      if (active.length) segments.push(active)
      active = []
      return
    }
    active.push(point(value, index))
  })
  if (active.length) segments.push(active)
  const allPoints = segments.flat()
  return (
    <svg className="h-[4.75rem] w-full overflow-visible" viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" role="img" aria-label="14-Tage-Verlauf">
      <defs><linearGradient id={id} x1="0" x2="0" y1="0" y2="1"><stop offset="0%" stopColor={color} stopOpacity="0.24" /><stop offset="100%" stopColor={color} stopOpacity="0" /></linearGradient></defs>
      <path d={`M ${padding} ${height - 1} H ${width - padding}`} stroke="currentColor" className="text-border" strokeWidth="1" />
      {segments.map((segment, index) => segment.length > 1 ? (
        <g key={index}>
          <path d={`M ${segment[0].x} ${height} L ${segment.map(({ x, y }) => `${x} ${y}`).join(" L ")} L ${segment.at(-1)?.x} ${height} Z`} fill={`url(#${id})`} />
          <path d={`M ${segment.map(({ x, y }) => `${x} ${y}`).join(" L ")}`} fill="none" stroke={color} strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" vectorEffect="non-scaling-stroke" />
        </g>
      ) : <circle key={index} cx={segment[0].x} cy={segment[0].y} r="2.5" fill={color} />)}
      {allPoints.length ? <circle cx={allPoints.at(-1)?.x} cy={allPoints.at(-1)?.y} r="3.2" fill={color} stroke="var(--card)" strokeWidth="2" /> : null}
    </svg>
  )
}

function TrendPanel({ label, value, detail, series, color, icon: Icon, id }: { label: string; value: string; detail: string; series: Array<number | null | undefined>; color: string; icon: React.ComponentType<{ className?: string; style?: React.CSSProperties }>; id: string }) {
  return (
    <div className="min-w-0 rounded-xl border bg-background/28 p-3.5">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2 text-[0.62rem] font-semibold uppercase tracking-[0.15em]" style={{ color }}><Icon className="size-3.5" />{label}</div>
          <div className="mt-2 text-xl font-semibold tracking-tight" style={{ color }}>{value}</div>
        </div>
        <span className="text-[0.58rem] font-medium uppercase tracking-wider text-muted-foreground">14 Tage</span>
      </div>
      <div className="mt-2"><Sparkline series={series} color={color} id={id} /></div>
      <p className="mt-1 truncate text-[0.66rem] text-muted-foreground">{detail}</p>
    </div>
  )
}

function DashboardHeader({ block, updatedAt, loading, onRefresh }: { block: BlockPayload | null; updatedAt: Date | null; loading: boolean; onRefresh: () => void }) {
  return (
    <header className="mb-4 flex items-start justify-between gap-3 border-b pb-4 sm:mb-5 sm:items-end sm:gap-4 sm:pb-5">
      <div className="min-w-0">
        <p className="text-[0.65rem] font-semibold uppercase tracking-[0.18em] text-primary">{formatLongDate()}</p>
        <div className="mt-1.5 flex flex-wrap items-baseline gap-x-3 gap-y-1">
          <h1 className="text-3xl font-semibold tracking-[-0.04em] sm:text-4xl">Heute</h1>
        </div>
        <p className="mt-1 max-w-3xl truncate text-xs text-muted-foreground sm:text-sm">
          {block?.status_line || block?.plan_name || "Training, Recovery und Ernährung in einem Blick"}
        </p>
      </div>
      <div className="mt-5 flex shrink-0 items-center gap-2 sm:mt-0">
        {updatedAt ? <span className="mr-1 hidden text-[0.68rem] text-muted-foreground sm:inline">Stand {updatedAt.toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" })}</span> : null}
        <Tooltip>
          <TooltipTrigger asChild>
            <Button variant="outline" size="icon" onClick={onRefresh} disabled={loading} aria-label="Dashboard aktualisieren">
              <RefreshCw className={loading ? "animate-spin" : ""} />
            </Button>
          </TooltipTrigger>
          <TooltipContent>Aktualisieren</TooltipContent>
        </Tooltip>
      </div>
    </header>
  )
}

function TodayDecision({ data, loading }: { data: DashboardData; loading: boolean }) {
  const recovery = data.signals?.context?.recovery?.today
  const recoveryAvg = data.signals?.context?.recovery?.avg_7d
  const plannedKcal = data.nutrition?.planned_totals?.kcal
  const targetKcal = data.nutrition?.targets?.kcal
  const loadMomentum = data.today?.load?.momentum_pct
  const recoverySeries = data.signals?.context?.recovery?.series || []
  const energySeries = data.signals?.context?.energy?.series || []
  const loadSeries = data.signals?.context?.load?.series || []
  const dayLabel = data.today?.today?.session_label || "Tagesform"

  return (
    <AppCard tone="today" className="xl:col-span-7">
      <CardHeading eyebrow="Heute" title={dayLabel} meta="Tagesform" tone="today" />
      {loading ? <CardLoading lines={2} /> : (
        <CardContent>
          <div className="grid gap-2.5 sm:grid-cols-3">
            <TrendPanel
              label="Recovery"
              value={recovery == null ? "—" : `${round(recovery)}`}
              detail={recoveryAvg == null ? "kein 7T-Mittel" : `7T Ø ${round(recoveryAvg)}`}
              series={recoverySeries}
              color="var(--domain-recovery)"
              icon={HeartPulse}
              id="recovery-gradient"
            />
            <TrendPanel
              label="Energie"
              value={plannedKcal == null ? "—" : `${round(plannedKcal)}`}
              detail={targetKcal == null ? "kein Ziel" : `Ziel ${round(targetKcal)} kcal`}
              series={energySeries}
              color="var(--domain-nutrition)"
              icon={Zap}
              id="energy-gradient"
            />
            <TrendPanel
              label="Load"
              value={loadMomentum == null ? "—" : formatSignedPercent(loadMomentum)}
              detail="gegen vorheriges Mittel"
              series={loadSeries}
              color="var(--domain-training)"
              icon={Activity}
              id="load-gradient"
            />
          </div>
        </CardContent>
      )}
    </AppCard>
  )
}

function AgendaCard({ agenda, loading }: { agenda: AgendaPayload | null; loading: boolean }) {
  const items = agenda?.items || []
  const [showMeals, setShowMeals] = useState(() => {
    try {
      return window.localStorage.getItem(AGENDA_MEALS_STORAGE_KEY) !== "false"
    } catch {
      return true
    }
  })
  const visibleItems = showMeals ? items : items.filter((item) => item.kind !== "meal")
  const setMealsVisible = (visible: boolean) => {
    setShowMeals(visible)
    try {
      window.localStorage.setItem(AGENDA_MEALS_STORAGE_KEY, String(visible))
    } catch {
      // The filter still works for this visit when storage is unavailable.
    }
  }
  return (
    <AppCard className="border-t-primary/45 xl:col-span-4">
      <CardHeading
        eyebrow="Zeitplan"
        title="Als Nächstes"
        tone="today"
        action={(
          <label className="flex shrink-0 items-center gap-1.5 rounded-md border border-border/60 px-2 py-1 text-[0.6rem] font-medium text-muted-foreground/80">
            <span>Meals</span>
            <Switch
              checked={showMeals}
              onCheckedChange={setMealsVisible}
              aria-label="Meal-Zeiten im Zeitplan anzeigen"
              size="sm"
            />
          </label>
        )}
      />
      {loading ? <CardLoading lines={4} /> : (
        <CardContent className="space-y-0">
          {visibleItems.length ? visibleItems.map((item, index) => (
            <div
              className={`grid grid-cols-[3.9rem_minmax(0,1fr)] gap-3 border-b py-3 first:pt-0 last:border-0 last:pb-0 ${item.kind === "school" ? "agenda-item-school" : ""}`}
              key={`${item.time_label}-${item.title}-${index}`}
            >
              <div className={`agenda-time font-mono text-[0.7rem] font-semibold ${item.kind === "calendar" ? "text-primary" : "text-muted-foreground"}`}>{item.time_label || "Ganztägig"}</div>
              <div className="min-w-0">
                <p className="truncate text-xs font-semibold sm:text-sm">{item.title || "Termin"}</p>
                <p className="mt-0.5 truncate text-[0.68rem] text-muted-foreground">{item.subtitle || item.source_label || ""}</p>
              </div>
            </div>
          )) : <p className="py-8 text-center text-sm text-muted-foreground">{items.length ? "Keine Termine mit diesem Filter." : "Heute stehen keine Termine an."}</p>}
        </CardContent>
      )}
    </AppCard>
  )
}

function SignalsCard({ signals, loading }: { signals: SignalsPayload | null; loading: boolean }) {
  const days = (signals?.days || []).slice(-14)
  const strength = (signals?.lanes?.strength || []).slice(-14)
  const cardio = (signals?.lanes?.run_load || []).slice(-14)
  const isActive = (item?: SignalItem) => ["present", "done", "good", "green"].includes(String(item?.state || "").toLowerCase())
  const activityDays = days.map((day, index) => ({ day, strength: isActive(strength[index]), cardio: isActive(cardio[index]) }))
  const strengthDays = activityDays.filter((day) => day.strength).length
  const cardioDays = activityDays.filter((day) => day.cardio).length
  const restDays = activityDays.filter((day) => !day.strength && !day.cardio).length
  return (
    <AppCard tone="recovery" className="xl:col-span-5">
      <CardHeading eyebrow="Letzte 14 Tage" title="Aktivitätsbilanz" meta="Rhythmus" tone="recovery" />
      {loading ? <CardLoading lines={2} /> : (
        <CardContent className="space-y-4">
          <div className="grid grid-cols-3 gap-2">
            <div className="rounded-xl border bg-background/30 p-3"><Dumbbell className="size-4 text-primary" /><p className="mt-3 text-2xl font-semibold text-primary">{strengthDays}</p><p className="text-[0.65rem] text-muted-foreground">Krafttage</p></div>
            <div className="rounded-xl border bg-background/30 p-3"><Route className="size-4 text-[var(--domain-recovery)]" /><p className="mt-3 text-2xl font-semibold text-[var(--domain-recovery)]">{cardioDays}</p><p className="text-[0.65rem] text-muted-foreground">Lauftage</p></div>
            <div className="rounded-xl border bg-background/30 p-3"><Moon className="size-4 text-muted-foreground" /><p className="mt-3 text-2xl font-semibold">{restDays}</p><p className="text-[0.65rem] text-muted-foreground">freie Tage</p></div>
          </div>
          <div className="grid grid-cols-7 gap-1.5 rounded-xl border bg-background/30 p-3 sm:grid-cols-14 sm:gap-2">
            {activityDays.map((entry, index) => (
              <div className={`${index < 7 ? "hidden sm:flex" : "flex"} min-w-0 flex-col items-center gap-2`} key={entry.day} title={`${entry.day} · ${entry.strength ? "Kraft" : ""}${entry.strength && entry.cardio ? " + " : ""}${entry.cardio ? "Cardio" : ""}${!entry.strength && !entry.cardio ? "Frei" : ""}`}>
                <div className={`flex h-10 w-full items-center justify-center gap-1 rounded-lg border ${entry.strength || entry.cardio ? "border-primary/25 bg-primary/8" : "bg-muted/25"}`}>
                  {entry.strength ? <Dumbbell className="size-3.5 text-primary" /> : null}
                  {entry.cardio ? <Route className="size-3.5 text-[var(--domain-recovery)]" /> : null}
                  {!entry.strength && !entry.cardio ? <span className="size-1.5 rounded-full bg-muted-foreground/30" /> : null}
                </div>
                <span className="text-[0.52rem] text-muted-foreground">{weekdayLabel(entry.day)}</span>
              </div>
            ))}
          </div>
        </CardContent>
      )}
    </AppCard>
  )
}

function NutritionCard({ nutrition, loading, onRefresh, readOnly = false }: { nutrition: NutritionPayload | null; loading: boolean; onRefresh: () => void; readOnly?: boolean }) {
  const meals = nutrition?.planned_meals || []
  const logged = number(nutrition?.logged_totals?.kcal)
  const target = number(nutrition?.targets?.kcal)
  const planned = number(nutrition?.planned_totals?.kcal)
  const progress = target > 0 ? Math.min(100, Math.round((logged / target) * 100)) : 0
  const [loggingSlot, setLoggingSlot] = useState<number | null>(null)
  const [feedback, setFeedback] = useState<{ slot: number; type: "success" | "error"; text: string } | null>(null)
  const isLogged = (meal: Meal) => Boolean(meal.logged_meal_id) || ["logged", "telegram_confirmed", "manual_override", "adjusted_by_core"].includes(String(meal.status || ""))
  const logMeal = async (meal: Meal) => {
    const slotId = number(meal.slot_id)
    if (readOnly || !slotId || loggingSlot || isLogged(meal)) return
    setLoggingSlot(slotId)
    setFeedback(null)
    try {
      const response = await fetch(`/api/nutrition/logging/planned/${slotId}/log`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ date: nutrition?.date || berlinDayIso(), time_mode: "planned" }),
      })
      const payload = await response.json().catch(() => null)
      if (!response.ok || payload?.ok === false || payload?.blocked_write) throw new Error(payload?.message || payload?.error || "Meal konnte nicht geloggt werden.")
      setFeedback({ slot: slotId, type: "success", text: "Meal geloggt" })
      onRefresh()
    } catch (error) {
      setFeedback({ slot: slotId, type: "error", text: error instanceof Error ? error.message : "Logging fehlgeschlagen" })
    } finally {
      setLoggingSlot(null)
    }
  }
  return (
    <AppCard tone="nutrition" className="xl:col-span-8">
      <CardHeading eyebrow="Ernährung" title="Meals & Zutaten" meta={target ? `${round(target)} kcal` : undefined} tone="nutrition" />
      {loading ? <CardLoading lines={3} /> : (
        <CardContent className="space-y-4">
          <div>
            <div className="mb-2 flex items-end justify-between gap-3">
              <div><span className="text-2xl font-semibold tracking-tight">{round(logged)}</span><span className="ml-1 text-xs text-muted-foreground">geloggt</span></div>
              <span className="text-[0.68rem] text-muted-foreground">{planned ? `${round(planned)} geplant` : "Noch nicht geplant"}</span>
            </div>
            <Progress className="[&_[data-slot=progress-indicator]]:bg-[var(--domain-nutrition)]" value={progress} aria-label={`${progress} Prozent des Kalorienziels geloggt`} />
          </div>
          <div className="space-y-2">
            {meals.map((meal) => {
              const slotId = number(meal.slot_id)
              const done = isLogged(meal) || (feedback?.slot === slotId && feedback.type === "success")
              return (
                <details className={`group overflow-hidden rounded-xl border transition-colors ${done ? "border-border/70 bg-muted/20" : "bg-background/25"}`} key={meal.slot_id || meal.title}>
                  <summary className="grid cursor-pointer list-none grid-cols-[3.25rem_minmax(0,1fr)_auto_auto_auto] items-center gap-2 px-3 py-3 marker:hidden">
                    <span className="font-mono text-[0.68rem] text-muted-foreground">{meal.time_text || "—"}</span>
                    <span className={`truncate text-xs font-semibold sm:text-sm ${done ? "text-foreground/70" : ""}`}>{meal.title || "Meal"}</span>
                    {done ? <Check className="size-3.5 text-[var(--domain-nutrition)]" aria-label="Geloggt" /> : null}
                    <span className="text-[0.64rem] text-muted-foreground">{round(meal.macros?.kcal)} kcal</span>
                    <ChevronDown className="size-3.5 text-muted-foreground transition-transform group-open:rotate-180" />
                  </summary>
                  <div className="border-t px-3 pb-3 pt-2.5">
                    <div className="space-y-1.5">
                      {(meal.items || []).map((item, index) => (
                        <div className="flex items-baseline justify-between gap-3 text-xs" key={`${item.food_name}-${index}`}>
                          <span className="min-w-0 truncate text-muted-foreground">{item.food_name || "Zutat"}</span>
                          <span className="shrink-0 font-mono text-foreground">{number(item.amount)} {item.unit || "g"}</span>
                        </div>
                      ))}
                    </div>
                    <div className="mt-3 grid grid-cols-3 gap-2 border-t pt-3 text-center">
                      <span className="text-[0.62rem] text-muted-foreground"><strong className="block text-xs text-foreground">{round(meal.macros?.p)} g</strong>Protein</span>
                      <span className="text-[0.62rem] text-muted-foreground"><strong className="block text-xs text-foreground">{round(meal.macros?.c)} g</strong>Carbs</span>
                      <span className="text-[0.62rem] text-muted-foreground"><strong className="block text-xs text-foreground">{round(meal.macros?.f)} g</strong>Fett</span>
                    </div>
                    <Button
                      className={`mt-3 w-full ${done
                        ? "border-[var(--domain-nutrition)]/35 bg-[var(--domain-nutrition)]/10 text-[var(--domain-nutrition)] hover:bg-[var(--domain-nutrition)]/15"
                        : "border-[var(--domain-nutrition)] bg-[var(--domain-nutrition)] text-background hover:bg-[var(--domain-nutrition)]/90"}`}
                      size="sm"
                      variant={done ? "outline" : "default"}
                      disabled={readOnly || done || loggingSlot === slotId}
                      onClick={() => logMeal(meal)}
                    >
                      {readOnly ? "Nur lesen" : loggingSlot === slotId ? <><LoaderCircle className="animate-spin" /> Wird geloggt</> : done ? <><Check /> Geloggt</> : "Meal jetzt loggen"}
                    </Button>
                    {feedback?.slot === slotId && feedback.type === "error" ? <p className="mt-2 text-center text-[0.68rem] text-destructive">{feedback.text}</p> : null}
                  </div>
                </details>
              )
            })}
          </div>
          <div className="grid grid-cols-3 divide-x rounded-lg bg-muted/30 px-1 py-2 text-center">
            <div><p className="text-xs font-semibold">{round(nutrition?.planned_totals?.p)} g</p><p className="text-[0.58rem] uppercase tracking-wider text-muted-foreground">Protein</p></div>
            <div><p className="text-xs font-semibold">{round(nutrition?.planned_totals?.c)} g</p><p className="text-[0.58rem] uppercase tracking-wider text-muted-foreground">Carbs</p></div>
            <div><p className="text-xs font-semibold">{round(nutrition?.planned_totals?.f)} g</p><p className="text-[0.58rem] uppercase tracking-wider text-muted-foreground">Fett</p></div>
          </div>
        </CardContent>
      )}
      <CardFooter className="justify-end"><Button asChild variant="ghost" size="sm"><a href="/mfp">Ernährung loggen <ChevronRight data-icon="inline-end" /></a></Button></CardFooter>
    </AppCard>
  )
}

function TrainingCard({ training, loading }: { training: TrainingPayload | null; loading: boolean }) {
  const exercises = training?.planned_session?.exercises || []
  const coaching = training?.items || []
  const hasCoaching = coaching.length > 0
  const rows = hasCoaching
    ? coaching.map((item) => ({
        name: item.exercise || "Übung",
        target: item.left || "—",
        note: item.middle_note || "Keine zusätzliche Anweisung.",
        badge: item.badge || item.action || "Coaching",
      }))
    : exercises.map((exercise) => ({
        name: exercise.name || "Übung",
        target: `${exercise.planned_sets || "—"} × ${exercise.target_rep_range || "—"}`,
        note: exercise.notes || exercise.equipment || "Keine zusätzliche Anweisung.",
        badge: `${exercise.planned_sets || "—"} × ${exercise.target_rep_range || "—"}`,
      }))
  const isPending = loading || trainingCardIsPending(training)
  const emptyMessage = training?.fallback?.message
    || (training?.status === "not_applicable" ? "Heute ist keine Krafteinheit geplant." : "Für die nächste Einheit sind noch keine Übungen verfügbar.")
  return (
    <AppCard tone="training" className="xl:col-span-8">
      <CardHeading eyebrow={hasCoaching ? "GPT-Coaching" : "Training"} title={training?.planned_session?.name || "Nächste Einheit"} meta={training?.planned_session?.weekday} tone="training" />
      {isPending ? <CardLoading lines={4} /> : (
        <CardContent>
          {rows.length ? (
            <div className="divide-y rounded-xl border px-3 sm:px-4">
              {rows.slice(0, 4).map((exercise, index) => (
                <div className="grid grid-cols-[1.4rem_minmax(0,1fr)_auto] items-center gap-3 py-3" key={`${exercise.name}-${index}`}>
                  <span className="font-mono text-[0.65rem] text-muted-foreground">{String(index + 1).padStart(2, "0")}</span>
                  <div className="min-w-0">
                    <p className="truncate text-xs font-semibold sm:text-sm">{exercise.name}</p>
                    <p className="mt-0.5 hidden truncate font-mono text-[0.67rem] text-muted-foreground sm:block">{exercise.target}</p>
                  </div>
                  <Badge variant="secondary" className="max-w-36 truncate font-mono">{exercise.badge}</Badge>
                </div>
              ))}
            </div>
          ) : <div className="grid min-h-28 place-items-center rounded-xl border border-dashed px-4 text-center text-xs text-muted-foreground">{emptyMessage}</div>}
          {rows.length > 4 ? <p className="mt-3 text-[0.68rem] text-muted-foreground">+ {rows.length - 4} weitere Übungen in der Trainingsansicht</p> : null}
        </CardContent>
      )}
      <CardFooter className="justify-between gap-3">
        <span className="truncate text-[0.68rem] text-muted-foreground">{training?.subtitle || "Planauflösung aktuell"}</span>
        <Dialog>
          <DialogTrigger asChild><Button variant="outline" size="sm">Trainingsansicht <ChevronRight data-icon="inline-end" /></Button></DialogTrigger>
          <DialogContent className="grid max-h-[92svh] grid-rows-[auto_minmax(0,1fr)_auto] gap-0 overflow-hidden border-border bg-popover p-0 shadow-[0_32px_90px_rgba(0,0,0,0.58)] max-sm:inset-0 max-sm:h-svh max-sm:max-h-none max-sm:max-w-none max-sm:translate-x-0 max-sm:translate-y-0 max-sm:rounded-none sm:max-w-4xl sm:rounded-lg">
            <DialogHeader className="border-b border-border bg-card px-5 py-5 pr-14 sm:px-6">
              <DialogDescription className="font-mono text-[0.68rem] font-semibold uppercase tracking-[0.18em] text-primary">Trainingsbriefing · Heute</DialogDescription>
              <DialogTitle className="text-xl font-semibold leading-tight tracking-[-0.02em] sm:text-2xl">{training?.planned_session?.name || "Training"}</DialogTitle>
              <p className="text-xs text-muted-foreground">Zielbereiche, Laststeuerung und Ausführungshinweise</p>
            </DialogHeader>
            <div className="overflow-y-auto p-3 sm:p-5">
              <div className="divide-y divide-border overflow-hidden border border-border bg-card sm:rounded-md">
                {rows.map((exercise, index) => (
                  <section className="grid grid-cols-[2rem_minmax(0,1fr)] gap-3 p-4 sm:grid-cols-[2.5rem_minmax(0,1fr)] sm:px-5" key={`${exercise.name}-detail-${index}`}>
                    <span className="font-mono text-[0.68rem] font-semibold text-primary">{String(index + 1).padStart(2, "0")}</span>
                    <div className="min-w-0">
                      <div className="flex items-start justify-between gap-4">
                        <div className="min-w-0"><h3 className="font-semibold text-foreground">{exercise.name}</h3><p className="mt-1 font-mono text-xs font-medium text-primary sm:text-sm">{exercise.target}</p></div>
                        <Badge className="shrink-0 rounded-md border-border bg-muted font-mono text-[0.62rem] text-foreground" variant="outline">{exercise.badge}</Badge>
                      </div>
                      <p className="mt-3 border-l-2 border-primary/55 pl-3 text-sm leading-relaxed text-muted-foreground">{exercise.note}</p>
                    </div>
                  </section>
                ))}
              </div>
            </div>
            <DialogFooter className="m-0 rounded-none border-t border-border bg-card px-5 py-3 sm:px-6"><Button asChild><a href="/training">Training öffnen <ArrowRight data-icon="inline-end" /></a></Button></DialogFooter>
          </DialogContent>
        </Dialog>
      </CardFooter>
    </AppCard>
  )
}

function EnduranceCard({ endurance, loading }: { endurance: EndurancePayload | null; loading: boolean }) {
  const session = endurance?.session
  const steps = session?.execution_steps || []
  const syncState = endurance?.sync?.state
  const isSynced = syncState === "synced"
  const syncText = isSynced
    ? "Intervals ✓ · Garmin via Intervals"
    : syncState === "sync_error"
      ? "Sync fehlgeschlagen"
      : "Noch nicht synchronisiert"

  return (
    <AppCard tone="endurance" className="xl:col-span-4">
      <CardHeading
        eyebrow="Cardio"
        title="Nächster Lauf"
        meta={session?.scheduled_date ? formatRunDate(session.scheduled_date) : undefined}
        tone="endurance"
      />
      {loading ? <CardLoading lines={3} /> : (
        <CardContent className="flex flex-1 flex-col gap-5">
          {session ? <>
            <div>
              <div className="mb-2 flex items-center gap-2 text-[0.68rem] font-semibold uppercase tracking-[0.12em] text-[var(--domain-endurance)]">
                <Route className="size-3.5" />
                <span className="truncate">{endurance?.phase?.name || endurance?.plan?.title || "Cardio-Plan"}</span>
              </div>
              <h3 className="text-xl font-semibold leading-tight tracking-[-0.025em] text-foreground">{session.title || "Geplanter Lauf"}</h3>
              <div className="mt-4 grid grid-cols-3 divide-x divide-border border-y border-border py-3">
                <div className="pr-3"><p className="text-[0.58rem] font-semibold uppercase tracking-wider text-muted-foreground">Dauer</p><p className="mt-1 font-mono text-sm font-semibold">{formatRunDuration(session.duration_s)}</p></div>
                <div className="px-3"><p className="text-[0.58rem] font-semibold uppercase tracking-wider text-muted-foreground">Distanz</p><p className="mt-1 font-mono text-sm font-semibold">{formatRunDistance(session.distance_m)}</p></div>
                <div className="pl-3"><p className="text-[0.58rem] font-semibold uppercase tracking-wider text-muted-foreground">Typ</p><p className="mt-1 truncate text-sm font-semibold">{runTypeLabel[session.session_type || ""] || session.session_type || "Lauf"}</p></div>
              </div>
            </div>
            {steps.length ? <div>
              <div className="mb-2 flex items-center justify-between text-[0.6rem] font-semibold uppercase tracking-[0.12em] text-muted-foreground">
                <span>Ablauf</span>
                <span>{steps.length} Schritte</span>
              </div>
              <div className="flex h-9 items-end gap-0.5 overflow-hidden rounded-md border border-border bg-muted/30 p-1" aria-label="Ablauf der Laufeinheit">
                {steps.map((step, index) => {
                  const duration = Math.max(number(step.duration_s, 30), 30)
                  const isFast = ["work", "interval", "stride"].includes(step.kind || "")
                  return <span
                    className={`min-w-1 rounded-[2px] ${runStepTone[step.kind || ""] || "bg-primary/45"}`}
                    key={`${step.id || step.kind}-${step.execution_index || index}-${index}`}
                    style={{ flexGrow: duration, flexBasis: 0, height: isFast ? "100%" : step.kind === "recovery" ? "42%" : "68%" }}
                    title={`${step.kind || "Schritt"} · ${formatStepDuration(step.duration_s)}`}
                  />
                })}
              </div>
              {session.notes ? <p className="mt-3 text-xs leading-relaxed text-muted-foreground">{session.notes}</p> : null}
            </div> : <div className="grid min-h-20 place-items-center rounded-xl border border-dashed px-4 text-center text-xs text-muted-foreground">Die Einheit hat noch keinen strukturierten Ablauf.</div>}
          </> : <div className="grid min-h-40 place-items-center rounded-xl border border-dashed px-4 text-center text-sm text-muted-foreground">Kein offener Lauf im lokalen Cardio-Plan.</div>}
        </CardContent>
      )}
      <CardFooter className="justify-between gap-3">
        <div className="flex min-w-0 items-center gap-2 text-[0.65rem] text-muted-foreground" title={endurance?.sync?.error || syncText}>
          <span className={`size-1.5 shrink-0 rounded-full ${isSynced ? "bg-[var(--status-good)]" : syncState === "sync_error" ? "bg-[var(--status-bad)]" : "bg-muted-foreground"}`} />
          <span className="truncate">{syncText}</span>
        </div>
        <Button asChild variant="ghost" size="sm"><a href="/planung?tab=cardio">Plan öffnen <ChevronRight data-icon="inline-end" /></a></Button>
      </CardFooter>
    </AppCard>
  )
}

function ContextStrip({ data }: { data: DashboardData }) {
  const lastTraining = data.today?.last_logs?.training
  const lastRun = data.today?.last_logs?.runs?.date
  const lastNutrition = data.today?.last_logs?.nutrition?.date_iso
  const entries = [
    { icon: Dumbbell, tone: "text-[var(--domain-training)]", label: "Gym", value: lastTraining?.date_iso ? `${lastTraining.name || "Einheit"} · ${lastTraining.date_iso.slice(5).split("-").reverse().join(".")}.` : "—" },
    { icon: Route, tone: "text-[var(--domain-endurance)]", label: "Run", value: lastRun ? new Date(lastRun).toLocaleDateString("de-DE", { day: "2-digit", month: "2-digit" }) : "—" },
    { icon: Apple, tone: "text-[var(--domain-nutrition)]", label: "Nutrition", value: lastNutrition ? new Date(`${lastNutrition}T12:00:00Z`).toLocaleDateString("de-DE", { day: "2-digit", month: "2-digit" }) : "—" },
  ]
  return (
    <div className="mb-4 grid grid-cols-3 divide-x overflow-hidden rounded-xl border bg-card/70 sm:mb-5 sm:w-fit sm:min-w-[30rem]">
      {entries.map(({ icon: Icon, tone, label, value }) => (
        <div className="flex min-w-0 items-center gap-2 px-3 py-2" key={label}>
          <Icon className={`hidden size-3.5 shrink-0 sm:block ${tone}`} />
          <div className="min-w-0"><p className="text-[0.55rem] font-semibold uppercase tracking-wider text-muted-foreground">{label}</p><p className="truncate text-[0.68rem] font-medium">{value}</p></div>
        </div>
      ))}
    </div>
  )
}

function DashboardApp() {
  const { data, loading, errors, updatedAt, refresh } = useDashboardData()
  const dashboardRoot = document.getElementById("liva-dashboard-root")
  const testMode = dashboardRoot?.dataset.testMode === "true"
  const readOnly = dashboardRoot?.dataset.authLevel === "key"
  const errorMessage = useMemo(() => errors.length ? `${errors.length} Datenquelle${errors.length === 1 ? "" : "n"} nicht erreichbar` : "", [errors])
  const anyLoading = Object.values(loading).some(Boolean)
  return (
    <TooltipProvider delayDuration={250}>
      <div className="min-h-svh bg-background text-foreground">
        <main className="shared-sidebar-main pb-24 sm:pb-0">
          <div className="mx-auto w-full max-w-[92rem] px-3 py-4 sm:px-5 sm:py-6 xl:px-8">
            {testMode ? <div className="mb-3 rounded-lg border border-[var(--status-warn)]/30 bg-[var(--status-warn)]/10 px-3 py-2 text-xs text-[var(--status-warn)]">Testmodus · keine echten Schreibaktionen</div> : null}
            {errorMessage ? <button type="button" onClick={refresh} className="mb-3 w-full rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-left text-xs text-destructive">{errorMessage} · Erneut laden</button> : null}
            <DashboardHeader block={data.block} updatedAt={updatedAt} loading={anyLoading} onRefresh={refresh} />
            <ContextStrip data={data} />
            <div className="grid grid-cols-1 gap-3 sm:gap-4 xl:grid-cols-12">
              <TodayDecision data={data} loading={(loading.today && !data.today) || (loading.signals && !data.signals)} />
              <SignalsCard signals={data.signals} loading={loading.signals && !data.signals} />
              <div className="grid grid-cols-1 gap-3 sm:gap-4 min-[700px]:grid-cols-2 xl:col-span-12 xl:grid-cols-12">
                <AgendaCard agenda={data.agenda} loading={loading.agenda && !data.agenda} />
                <NutritionCard nutrition={data.nutrition} loading={loading.nutrition && !data.nutrition} onRefresh={refresh} readOnly={readOnly} />
              </div>
              <div className="grid grid-cols-1 gap-3 sm:gap-4 min-[700px]:grid-cols-2 xl:col-span-12 xl:grid-cols-12">
                <EnduranceCard endurance={data.endurance} loading={loading.endurance && !data.endurance} />
                <TrainingCard training={data.training} loading={loading.training && !data.training} />
              </div>
            </div>
          </div>
        </main>
      </div>
    </TooltipProvider>
  )
}

export default DashboardApp
