/**
 * UI smoke test.
 *
 * Drives a real browser through the whole application to confirm each screen
 * renders with live data, the console stays clean, and the layout never scrolls
 * sideways at mobile width.
 *
 * Run with:  node e2e/ui.spec.mjs
 * Requires the backend on :8000 and the frontend dev server on :5173.
 */
import { chromium } from 'playwright'

const BASE = process.env.UI_BASE ?? 'http://127.0.0.1:5173'
const EMAIL = 'demo@ailedger.local'
const PASSWORD = 'demo1234'

const passed = []
const failed = []
const consoleErrors = []

function check(name, condition, detail = '') {
  ;(condition ? passed : failed).push(name)
  console.log(`  [${condition ? 'PASS' : 'FAIL'}] ${name}${detail ? ` — ${detail}` : ''}`)
  return condition
}

const ROUTES = [
  ['/', 'Overview', ['Cash', 'Revenue', 'Net profit']],
  ['/ai', 'AI Command Center', ['Ask your finance team']],
  ['/accounting/journals', 'Journal entries', ['Journal entries']],
  ['/accounting/accounts', 'Chart of accounts', ['Chart of accounts', 'ASSET']],
  ['/accounting/trial-balance', 'Trial balance', ['Trial balance']],
  ['/accounting/ledger', 'General ledger', ['General ledger']],
  ['/banking', 'Bank accounts', ['Bank accounts']],
  ['/banking/transactions', 'Bank transactions', ['Bank transactions']],
  ['/banking/reconciliation', 'Reconciliation', ['reconciliation']],
  ['/sales/invoices', 'Invoices', ['Invoices']],
  ['/sales/customers', 'Customers', ['Customers']],
  ['/sales/receipts', 'Receipts', ['Receipts']],
  ['/purchases/bills', 'Bills', ['Bills']],
  ['/purchases/vendors', 'Vendors', ['Vendors']],
  ['/purchases/payments', 'Payments', ['Payments']],
  ['/inventory', 'Inventory', ['Inventory', 'Stock value']],
  ['/tax/gst', 'GST', ['GST', 'Output GST']],
  ['/tax/tds', 'TDS', ['TDS']],
  ['/tax/edocs', 'e-Invoice', ['e-Way Bill']],
  ['/reports', 'Reports', ['Profit & Loss']],
  ['/cash-flow', 'Cash flow', ['Cash flow forecast']],
  ['/close', 'Close', ['Month-end close']],
  ['/agents', 'AI agents', ['AI agents']],
  ['/exceptions', 'Exceptions', ['Needs your attention']],
  ['/audit', 'Audit log', ['Audit log']],
  ['/settings', 'Settings', ['Settings']],
]

const browser = await chromium.launch()
const context = await browser.newContext({ viewport: { width: 1440, height: 900 } })
const page = await context.newPage()

page.on('console', (message) => {
  if (message.type() === 'error') {
    const text = message.text()
    // React DevTools nag and favicon 404s are noise, not defects.
    if (!/DevTools|favicon|ResizeObserver/i.test(text)) {
      consoleErrors.push(`${page.url()}: ${text.slice(0, 160)}`)
    }
  }
})
page.on('pageerror', (error) => {
  consoleErrors.push(`${page.url()}: ${String(error).slice(0, 160)}`)
})

try {
  console.log('\n=== LOGIN ===')
  await page.goto(`${BASE}/login`, { waitUntil: 'networkidle' })
  check('login page renders', await page.getByText('Your books, managed by AI.').isVisible())
  check(
    'demo credentials pre-filled',
    (await page.locator('input[type=email]').inputValue()) === EMAIL,
  )

  await page.locator('input[type=email]').fill(EMAIL)
  await page.locator('input[type=password]').fill(PASSWORD)
  await page.getByRole('button', { name: 'Sign in' }).click()
  await page.waitForURL(`${BASE}/`, { timeout: 20000 })
  check('signs in and lands on the dashboard', page.url() === `${BASE}/`)

  console.log('\n=== DASHBOARD ===')
  await page.waitForSelector('text=Good morning', { timeout: 20000 })
  // The invariant footer is the last thing the dashboard query renders, so it
  // is the only reliable signal that real data has arrived. Waiting on a "₹"
  // is not enough: an unloaded KPI still formats as "₹0".
  await page.waitForFunction(
    () => /accounting invariants/.test(document.querySelector('main')?.innerText ?? ''),
    { timeout: 30000 },
  )
  const body = await page.locator('main').innerText()
  check('AI briefing rendered', body.includes('Good morning'))
  check('rupee amounts rendered', /₹[\d,]+/.test(body), body.match(/₹[\d,.]+/)?.[0])
  check('invariants confirmed on screen', body.includes('accounting invariants pass'))
  check('briefing formats money for reading', !/\d{7,}\.\d{2}/.test(body),
    body.match(/\d{7,}\.\d{2}/)?.[0] ?? '')
  check('briefing formats percentages', !/\d\.\d{5,}%/.test(body),
    body.match(/[\d.]+%/)?.[0] ?? '')

  console.log('\n=== EVERY ROUTE ===')
  for (const [route, label, expectations] of ROUTES) {
    await page.goto(`${BASE}${route}`, { waitUntil: 'networkidle' })
    await page.waitForTimeout(700)
    const text = await page.locator('body').innerText()
    const found = expectations.filter((phrase) =>
      text.toLowerCase().includes(phrase.toLowerCase()),
    )
    check(
      `${label} (${route})`,
      found.length > 0 && !text.includes('Could not load this'),
      found.length === 0 ? `expected one of ${expectations.join(', ')}` : '',
    )
  }

  console.log('\n=== INTERACTION ===')
  await page.goto(`${BASE}/ai`, { waitUntil: 'networkidle' })
  await page.locator('textarea').first().fill('What is our cash position?')
  await page.keyboard.press('Enter')
  await page.waitForSelector('text=/Cash and bank balances total/', { timeout: 30000 })
  const answer = await page.locator('body').innerText()
  check('copilot answers in the UI', /Cash and bank balances total ₹/.test(answer))
  // The tool badge renders with underscores replaced by spaces.
  check('tool provenance shown', /get cash position/i.test(answer))

  await page.goto(`${BASE}/sales/invoices`, { waitUntil: 'networkidle' })
  await page.waitForTimeout(800)
  const rows = await page.locator('tbody tr').count()
  check('invoice table populated', rows > 5, `${rows} rows`)

  await page.locator('tbody tr').first().click()
  await page.waitForSelector('[role=dialog]', { timeout: 10000 })
  // The journal entry loads in a second request; wait for it.
  await page
    .waitForFunction(
      () => /Balanced/i.test(document.querySelector('[role=dialog]')?.innerText ?? ''),
      { timeout: 15000 },
    )
    .catch(() => {})
  const dialog = await page.locator('[role=dialog]').innerText()
  check('invoice detail opens', dialog.includes('Invoice'))
  check('journal entry shown in detail', /journal entry/i.test(dialog))
  check('journal entry balances on screen', /Balanced/i.test(dialog))
  await page.keyboard.press('Escape')

  console.log('\n=== COMMAND PALETTE ===')
  await page.goto(`${BASE}/`, { waitUntil: 'networkidle' })
  await page.keyboard.press('Control+k')
  await page.waitForSelector('[role=dialog][aria-label=Search]', { timeout: 5000 })
  check('command palette opens with Ctrl+K', true)
  await page.keyboard.type('Aurora')
  await page.waitForTimeout(1200)
  const palette = await page.locator('[role=dialog][aria-label=Search]').innerText()
  check('search returns results', palette.toLowerCase().includes('aurora'), palette.slice(0, 80))
  await page.keyboard.press('Escape')

  console.log('\n=== DARK MODE ===')
  await page.getByRole('button', { name: /Switch to dark theme/i }).click()
  await page.waitForTimeout(400)
  const isDark = await page.evaluate(() =>
    document.documentElement.classList.contains('dark'),
  )
  check('dark mode toggles', isDark)
  const darkBg = await page.evaluate(() => getComputedStyle(document.body).backgroundColor)
  check('dark background applied', darkBg !== 'rgb(248, 250, 252)', darkBg)
  await page.getByRole('button', { name: /Switch to light theme/i }).click()

  console.log('\n=== RESPONSIVE ===')
  const mobile = await context.newPage()
  await mobile.setViewportSize({ width: 375, height: 812 })
  for (const route of ['/', '/sales/invoices', '/reports', '/close']) {
    await mobile.goto(`${BASE}${route}`, { waitUntil: 'networkidle' })
    await mobile.waitForTimeout(600)
    const overflow = await mobile.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    )
    check(`no horizontal overflow at 375px on ${route}`, overflow <= 1, `${overflow}px`)
  }
  const drawerHidden = await mobile.evaluate(() => {
    const aside = document.querySelector('aside')
    return aside ? aside.getBoundingClientRect().left < 0 : false
  })
  check('sidebar collapses to a drawer on mobile', drawerHidden)
  await mobile.close()

  console.log('\n=== ACCESSIBILITY ===')
  await page.goto(`${BASE}/`, { waitUntil: 'networkidle' })
  const landmarks = await page.evaluate(() => ({
    nav: document.querySelectorAll('nav, [aria-label="Main navigation"]').length,
    main: document.querySelectorAll('main').length,
    h1: document.querySelectorAll('h1').length,
    unlabelledButtons: [...document.querySelectorAll('button')].filter(
      (button) =>
        !button.textContent?.trim() &&
        !button.getAttribute('aria-label') &&
        !button.getAttribute('title'),
    ).length,
    imagesWithoutAlt: [...document.querySelectorAll('img')].filter(
      (image) => !image.hasAttribute('alt'),
    ).length,
  }))
  check('navigation landmark present', landmarks.nav > 0)
  check('main landmark present', landmarks.main > 0)
  check('exactly one h1', landmarks.h1 === 1, String(landmarks.h1))
  check('all icon buttons labelled', landmarks.unlabelledButtons === 0, String(landmarks.unlabelledButtons))
  check('all images have alt text', landmarks.imagesWithoutAlt === 0)

  console.log('\n=== CONSOLE HEALTH ===')
  check(
    'no console errors across the app',
    consoleErrors.length === 0,
    consoleErrors.slice(0, 3).join(' | '),
  )
} catch (error) {
  failed.push(`fatal: ${error.message}`)
  console.log(`\n  [FATAL] ${error.message}`)
  await page.screenshot({ path: 'e2e/failure.png' }).catch(() => {})
} finally {
  await browser.close()
}

console.log('\n' + '='.repeat(66))
console.log(`  PASSED: ${passed.length}    FAILED: ${failed.length}`)
if (failed.length) {
  console.log('\n  Failures:')
  failed.forEach((name) => console.log(`    - ${name}`))
}
console.log('='.repeat(66))
process.exit(failed.length ? 1 : 0)
