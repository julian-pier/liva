import { useState } from "react"
import {
  Activity,
  ArrowUpRight,
  Check,
  ChevronDown,
  Moon,
  MoreHorizontal,
  Sun,
} from "lucide-react"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
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
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Progress } from "@/components/ui/progress"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"
import { Switch } from "@/components/ui/switch"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip"

const signalStates = [
  "good", "good", "good", "warn", "good", "bad", "bad",
  "warn", "good", "good", "good", "bad", "good", "good",
] as const

const signalColor = {
  good: "bg-[var(--status-good)]",
  warn: "bg-[var(--status-warn)]",
  bad: "bg-[var(--status-bad)]",
}

const dashboardPalette = [
  {
    label: "Flächen & Typografie",
    tokens: [
      ["--background", "#0e1013", "#e7ecf2"],
      ["--card", "#1a1d22", "#f3f7fb"],
      ["--popover", "#1f2329", "#f3f7fb"],
      ["--foreground", "#e7e9ee", "#18212f"],
      ["--muted-foreground", "#a1a7b3", "#6d7b90"],
    ],
  },
  {
    label: "Akzente & Status",
    tokens: [
      ["--primary", "#80cdf7", "#126a9b"],
      ["--status-good", "#2ce4aa", "#26c99a"],
      ["--status-warn", "#ffd25a", "#f2bb48"],
      ["--status-bad", "#ef7378", "#e8676b"],
      ["--domain-nutrition", "#ffd25a", "#d79418"],
      ["--domain-training", "#76a9ff", "#217fab"],
    ],
  },
]

function DashboardPalettePanel() {
  return (
    <div className="space-y-4">
      <Card>
        <CardHeader>
          <CardDescription>Dashboard-Referenz · Systemmemo #system</CardDescription>
          <CardTitle>Palette & Grundkomponenten</CardTitle>
          <CardDescription className="max-w-2xl leading-relaxed">
            Verbindliches Zielbild für neue Dashboard- und Subpage-Oberflächen: ruhige neutrale Flächen, klare Hierarchie und semantische Akzente mit hoher Zurückhaltung.
          </CardDescription>
        </CardHeader>
        <CardContent className="grid gap-4 lg:grid-cols-2">
          {dashboardPalette.map((group) => (
            <div className="space-y-2.5" key={group.label}>
              <p className="text-[0.65rem] font-semibold uppercase tracking-[0.16em] text-muted-foreground">{group.label}</p>
              <div className="space-y-2">
                {group.tokens.map(([name, darkValue, lightValue]) => (
                  <div className="flex items-center gap-3 rounded-lg border bg-muted/20 px-3 py-2.5" key={name}>
                    <div className="flex shrink-0 -space-x-1.5" aria-label={`${name} Dark- und Light-Wert`}>
                      <span className="size-7 rounded-md border-2 border-card" style={{ backgroundColor: darkValue }} title={`Dark ${darkValue}`} />
                      <span className="size-7 rounded-md border-2 border-card" style={{ backgroundColor: lightValue }} title={`Light ${lightValue}`} />
                    </div>
                    <div className="min-w-0 flex-1">
                      <p className="truncate font-mono text-[0.68rem] font-semibold">{name}</p>
                      <p className="mt-0.5 text-[0.62rem] text-muted-foreground">Dark {darkValue} · Light {lightValue}</p>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          ))}
        </CardContent>
      </Card>

      <div className="grid gap-4 md:grid-cols-3">
        {[
          ["Ecken", "0.75rem Basisradius", "12px Kartenradius"],
          ["Spacing", "kompakt, aber luftig", "globale mobile Skalierung"],
          ["Typografie", "Geist Variable", "Utility-Labels in Mono"],
        ].map(([label, value, detail]) => (
          <Card key={label}>
            <CardHeader className="pb-3"><CardDescription>{label}</CardDescription><CardTitle className="text-lg">{value}</CardTitle></CardHeader>
            <CardContent><p className="text-xs text-muted-foreground">{detail}</p></CardContent>
          </Card>
        ))}
      </div>
    </div>
  )
}

function SignalStrip() {
  return (
    <div className="grid grid-cols-14 gap-1.5" aria-label="Recovery der letzten 14 Tage">
      {signalStates.map((state, index) => (
        <span
          className={`aspect-square w-2.5 place-self-center rounded-[3px] ${signalColor[state]}`}
          key={`${state}-${index}`}
          title={`Tag ${index + 1}: ${state}`}
        />
      ))}
    </div>
  )
}

function FoundationPanel() {
  return (
    <div className="grid gap-4 lg:grid-cols-[1.15fr_0.85fr]">
      <Card>
        <CardHeader className="flex-row items-start justify-between gap-4">
          <div className="space-y-1">
            <CardDescription>Heute · Recovery</CardDescription>
            <CardTitle>Trainingsbereit</CardTitle>
          </div>
          <Badge variant="secondary">84 / 100</Badge>
        </CardHeader>
        <CardContent className="space-y-5">
          <div className="grid grid-cols-3 divide-x divide-border rounded-xl border bg-muted/30">
            {[
              ["HRV", "71 ms"],
              ["Schlaf", "8:04 h"],
              ["Load", "Normal"],
            ].map(([label, value]) => (
              <div className="min-w-0 px-3 py-3" key={label}>
                <p className="text-[0.65rem] font-semibold uppercase tracking-[0.16em] text-muted-foreground">{label}</p>
                <p className="mt-1 truncate text-sm font-semibold text-foreground">{value}</p>
              </div>
            ))}
          </div>
          <div className="space-y-2.5">
            <div className="flex items-center justify-between text-xs">
              <span className="font-medium">Letzte 14 Tage</span>
              <span className="text-muted-foreground">stabil</span>
            </div>
            <SignalStrip />
          </div>
          <Progress value={84} aria-label="Recovery 84 Prozent" />
        </CardContent>
        <CardFooter className="justify-between border-t">
          <p className="text-xs text-muted-foreground">Push B · 18:30 Uhr</p>
          <Button size="sm">Einheit ansehen <ArrowUpRight data-icon="inline-end" /></Button>
        </CardFooter>
      </Card>

      <Card>
        <CardHeader>
          <CardDescription>Systemzustände</CardDescription>
          <CardTitle>Semantische Tokens</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          {[
            ["Recovery", "Bereit", "bg-[var(--status-good)]"],
            ["Energy", "Beobachten", "bg-[var(--status-warn)]"],
            ["Training", "Blockiert", "bg-[var(--status-bad)]"],
            ["Daten", "Ausstehend", "bg-muted-foreground/35"],
          ].map(([label, value, color]) => (
            <div className="flex items-center gap-3 rounded-lg border px-3 py-2.5" key={label}>
              <span className={`size-2.5 rounded-full ${color}`} />
              <span className="flex-1 text-sm font-medium">{label}</span>
              <span className="text-xs text-muted-foreground">{value}</span>
            </div>
          ))}
        </CardContent>
      </Card>
    </div>
  )
}

function ControlsPanel() {
  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <Card>
        <CardHeader>
          <CardDescription>Aktionen</CardDescription>
          <CardTitle>Buttons und Menüs</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-wrap items-center gap-2">
          <Button><Check data-icon="inline-start" /> Speichern</Button>
          <Button variant="secondary">Sekundär</Button>
          <Button variant="outline">Abbrechen</Button>
          <Button variant="ghost" size="icon" aria-label="Mehr Optionen"><MoreHorizontal /></Button>
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button variant="outline">Zeitraum <ChevronDown data-icon="inline-end" /></Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="start">
              <DropdownMenuLabel>Auswertung</DropdownMenuLabel>
              <DropdownMenuSeparator />
              <DropdownMenuItem>Letzte 7 Tage</DropdownMenuItem>
              <DropdownMenuItem>Letzte 14 Tage</DropdownMenuItem>
              <DropdownMenuItem>Letzte 30 Tage</DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardDescription>Formulare</CardDescription>
          <CardTitle>Kontrollierte Eingaben</CardTitle>
        </CardHeader>
        <CardContent className="grid gap-4 sm:grid-cols-2">
          <div className="space-y-2">
            <Label htmlFor="ui-lab-target">Kalorienziel</Label>
            <Input id="ui-lab-target" inputMode="numeric" defaultValue="2850" />
          </div>
          <div className="space-y-2">
            <Label htmlFor="ui-lab-mode">Trainingsmodus</Label>
            <Select defaultValue="normal">
              <SelectTrigger id="ui-lab-mode"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="light">Leicht</SelectItem>
                <SelectItem value="normal">Normal</SelectItem>
                <SelectItem value="heavy">Schwer</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div className="flex items-center justify-between gap-4 rounded-lg border px-3 py-2.5 sm:col-span-2">
            <div>
              <p className="text-sm font-medium">Automatische Anpassung</p>
              <p className="text-xs text-muted-foreground">Plan anhand der Recovery anpassen</p>
            </div>
            <Switch aria-label="Automatische Anpassung" defaultChecked />
          </div>
        </CardContent>
      </Card>
    </div>
  )
}

function StatesPanel() {
  return (
    <div className="grid gap-4 md:grid-cols-3">
      {[
        ["Laden", "Daten werden vorbereitet"],
        ["Leer", "Noch keine Einheit vorhanden"],
        ["Fehler", "Polar konnte nicht erreicht werden"],
      ].map(([title, description], index) => (
        <Card key={title}>
          <CardHeader>
            <CardDescription>Zustand {index + 1}</CardDescription>
            <CardTitle>{title}</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            {index === 0 ? (
              <><Skeleton className="h-3 w-3/4" /><Skeleton className="h-3 w-1/2" /><Skeleton className="h-16 w-full" /></>
            ) : (
              <p className="min-h-16 text-sm leading-relaxed text-muted-foreground">{description}</p>
            )}
          </CardContent>
          <CardFooter><Button variant="outline" size="sm">{index === 2 ? "Erneut versuchen" : "Details"}</Button></CardFooter>
        </Card>
      ))}
    </div>
  )
}

function App() {
  const [dark, setDark] = useState(() => document.documentElement.classList.contains("dark"))

  const toggleTheme = () => {
    const next = !dark
    document.documentElement.classList.toggle("dark", next)
    setDark(next)
  }

  return (
    <TooltipProvider delayDuration={250}>
      <main className="min-h-svh bg-background text-foreground">
        <div className="mx-auto w-full max-w-6xl px-4 py-5 sm:px-6 sm:py-8">
          <header className="mb-6 flex flex-col gap-4 border-b pb-5 sm:flex-row sm:items-end sm:justify-between">
            <div>
              <div className="mb-2 flex items-center gap-2 text-[0.65rem] font-semibold uppercase tracking-[0.18em] text-muted-foreground">
                <Activity className="size-3.5 text-primary" /> LIVA Interface System
              </div>
              <h1 className="text-2xl font-semibold tracking-tight sm:text-3xl">Component laboratory</h1>
              <p className="mt-1.5 max-w-2xl text-sm text-muted-foreground">
                Produktive Tailwind- und shadcn-Basis für kompakte, responsive LIVA-Oberflächen.
              </p>
            </div>
            <div className="flex items-center gap-2">
              <Badge variant="outline">Tailwind v4</Badge>
              <Badge variant="outline">shadcn · Radix</Badge>
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button variant="outline" size="icon" onClick={toggleTheme} aria-label="Farbschema wechseln">
                    {dark ? <Sun /> : <Moon />}
                  </Button>
                </TooltipTrigger>
                <TooltipContent>Farbschema wechseln</TooltipContent>
              </Tooltip>
              <Dialog>
                <DialogTrigger asChild><Button size="sm">Dialog testen</Button></DialogTrigger>
                <DialogContent>
                  <DialogHeader>
                    <DialogTitle>Training bestätigen?</DialogTitle>
                    <DialogDescription>Die Einheit wird für heute als geplant übernommen.</DialogDescription>
                  </DialogHeader>
                  <DialogFooter>
                    <Button variant="outline">Abbrechen</Button>
                    <Button>Übernehmen</Button>
                  </DialogFooter>
                </DialogContent>
              </Dialog>
            </div>
          </header>

          <Tabs defaultValue="palette">
            <TabsList className="mb-4">
              <TabsTrigger value="palette">Dashboard-Palette</TabsTrigger>
              <TabsTrigger value="foundation">Foundation</TabsTrigger>
              <TabsTrigger value="controls">Controls</TabsTrigger>
              <TabsTrigger value="states">States</TabsTrigger>
            </TabsList>
            <TabsContent value="palette"><DashboardPalettePanel /></TabsContent>
            <TabsContent value="foundation"><FoundationPanel /></TabsContent>
            <TabsContent value="controls"><ControlsPanel /></TabsContent>
            <TabsContent value="states"><StatesPanel /></TabsContent>
          </Tabs>
        </div>
      </main>
    </TooltipProvider>
  )
}

export default App
