import { NavLink, Route, Routes } from 'react-router-dom'
import { AppProvider, useApp } from './context'
import { Health } from './components/Health'
import { Dashboard } from './pages/Dashboard'
import { RunPage } from './pages/RunPage'
import { SessionsPage } from './pages/SessionsPage'
import { SessionDetailPage } from './pages/SessionDetailPage'
import { DriversPage } from './pages/DriversPage'
import { SettingsPage } from './pages/SettingsPage'
import { SetupPage } from './pages/SetupPage'

const NAV = [
  { to: '/', label: 'Dashboard', icon: '◧', end: true },
  { to: '/run', label: 'New run', icon: '＋' },
  { to: '/sessions', label: 'Sessions', icon: '≣' },
  { to: '/drivers', label: 'Browser', icon: '◐' },
  { to: '/settings', label: 'Settings', icon: '⚙' },
  { to: '/setup', label: 'Setup', icon: '?' },
]

function Shell() {
  const { health } = useApp()
  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark">◤</span>
          <span>tabvis</span>
          <span className="brand-sub">console</span>
        </div>
        <nav className="nav">
          {NAV.map((n) => (
            <NavLink
              key={n.to}
              to={n.to}
              end={n.end}
              className={({ isActive }) => (isActive ? 'nav-item active' : 'nav-item')}
            >
              <span className="nav-ico">{n.icon}</span>
              <span>{n.label}</span>
            </NavLink>
          ))}
        </nav>
        <div className="sidebar-foot">
          <Health h={health} />
        </div>
      </aside>
      <main className="content">
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/run" element={<RunPage />} />
          <Route path="/sessions" element={<SessionsPage />} />
          <Route path="/sessions/:id" element={<SessionDetailPage />} />
          <Route path="/drivers" element={<DriversPage />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="/setup" element={<SetupPage />} />
          <Route path="*" element={<Dashboard />} />
        </Routes>
      </main>
    </div>
  )
}

export function App() {
  return (
    <AppProvider>
      <Shell />
    </AppProvider>
  )
}
