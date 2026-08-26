import type { ReactElement, ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { BrowserRouter, Navigate, Route, Routes, useLocation } from 'react-router-dom'
import { isAuthenticated } from '@/lib/api'
import { ToastProvider } from '@/components/ui'
import Layout from '@/components/Layout'

import Login from '@/pages/Login'
import Dashboard from '@/pages/Dashboard'
import CommandCenter from '@/pages/CommandCenter'
import Journals from '@/pages/Journals'
import ChartOfAccounts from '@/pages/ChartOfAccounts'
import TrialBalance from '@/pages/TrialBalance'
import GeneralLedger from '@/pages/GeneralLedger'
import BankAccounts from '@/pages/BankAccounts'
import BankTransactions from '@/pages/BankTransactions'
import Reconciliation from '@/pages/Reconciliation'
import Invoices from '@/pages/Invoices'
import Customers from '@/pages/Customers'
import Receipts from '@/pages/Receipts'
import Bills from '@/pages/Bills'
import Vendors from '@/pages/Vendors'
import Payments from '@/pages/Payments'
import Inventory from '@/pages/Inventory'
import GST from '@/pages/GST'
import TDS from '@/pages/TDS'
import EDocs from '@/pages/EDocs'
import Reports from '@/pages/Reports'
import CashFlow from '@/pages/CashFlow'
import Close from '@/pages/Close'
import Agents from '@/pages/Agents'
import Exceptions from '@/pages/Exceptions'
import AuditLog from '@/pages/AuditLog'
import SettingsPage from '@/pages/Settings'

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // Financial data must not be silently stale, but nor should every focus
      // change hammer the API.
      staleTime: 30_000,
      refetchOnWindowFocus: false,
      retry: (failureCount, error: any) => {
        if ([401, 403, 404, 409, 422].includes(error?.status)) return false
        return failureCount < 2
      },
    },
  },
})

function RequireAuth({ children }: { children: ReactNode }) {
  const location = useLocation()
  if (!isAuthenticated()) {
    return <Navigate to="/login" state={{ from: location.pathname }} replace />
  }
  return <Layout>{children}</Layout>
}

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <ToastProvider>
        <BrowserRouter>
          <Routes>
            <Route path="/login" element={<Login />} />
            {(
              [
                ['/', <Dashboard />],
                ['/ai', <CommandCenter />],
                ['/accounting/journals', <Journals />],
                ['/accounting/journals/:id', <Journals />],
                ['/accounting/accounts', <ChartOfAccounts />],
                ['/accounting/trial-balance', <TrialBalance />],
                ['/accounting/ledger', <GeneralLedger />],
                ['/accounting/ledger/:accountId', <GeneralLedger />],
                ['/banking', <BankAccounts />],
                ['/banking/transactions', <BankTransactions />],
                ['/banking/reconciliation', <Reconciliation />],
                ['/sales/invoices', <Invoices />],
                ['/sales/invoices/:id', <Invoices />],
                ['/sales/customers', <Customers />],
                ['/sales/customers/:id', <Customers />],
                ['/sales/receipts', <Receipts />],
                ['/purchases/bills', <Bills />],
                ['/purchases/bills/:id', <Bills />],
                ['/purchases/vendors', <Vendors />],
                ['/purchases/payments', <Payments />],
                ['/inventory', <Inventory />],
                ['/inventory/items/:id', <Inventory />],
                ['/tax/gst', <GST />],
                ['/tax/tds', <TDS />],
                ['/tax/edocs', <EDocs />],
                ['/reports', <Reports />],
                ['/cash-flow', <CashFlow />],
                ['/close', <Close />],
                ['/agents', <Agents />],
                ['/exceptions', <Exceptions />],
                ['/audit', <AuditLog />],
                ['/settings', <SettingsPage />],
              ] as [string, ReactElement][]
            ).map(([path, element]) => (
              <Route key={path} path={path} element={<RequireAuth>{element}</RequireAuth>} />
            ))}
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </BrowserRouter>
      </ToastProvider>
    </QueryClientProvider>
  )
}
