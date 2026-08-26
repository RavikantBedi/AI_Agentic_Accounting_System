/** Shared scaffolding for the many list-and-filter screens. */
import type { ReactNode } from 'react'
import { Search } from 'lucide-react'
import {
  Card,
  DataTable,
  EmptyState,
  Input,
  PageHeader,
  Pagination,
  Select,
  Toolbar,
  type Column,
} from '@/components/ui'

export interface FilterSpec {
  id: string
  label: string
  value: string
  onChange: (value: string) => void
  options: { value: string; label: string }[]
  width?: string
}

export function ListPage<T>({
  title,
  description,
  actions,
  columns,
  list,
  rowKey,
  onRowClick,
  searchPlaceholder = 'Search…',
  filters = [],
  emptyTitle,
  emptyDescription,
  emptyAction,
  emptyIcon,
  summary,
  children,
}: {
  title: string
  description?: string
  actions?: ReactNode
  columns: Column<T>[]
  list: {
    items: T[]
    total: number
    limit: number
    offset: number
    setOffset: (offset: number) => void
    search: string
    setSearch: (value: string) => void
    isLoading: boolean
  }
  rowKey: (row: T, index: number) => string
  onRowClick?: (row: T) => void
  searchPlaceholder?: string
  filters?: FilterSpec[]
  emptyTitle: string
  emptyDescription?: string
  emptyAction?: ReactNode
  emptyIcon?: any
  summary?: ReactNode
  children?: ReactNode
}) {
  return (
    <div>
      <PageHeader title={title} description={description} actions={actions}>
        <Toolbar>
          <div className="relative min-w-0 flex-1 sm:max-w-xs">
            <Search
              className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-subtle"
              aria-hidden
            />
            <Input
              value={list.search}
              onChange={(event) => list.setSearch(event.target.value)}
              placeholder={searchPlaceholder}
              className="pl-8"
              aria-label={searchPlaceholder}
            />
          </div>
          {filters.map((filter) => (
            <Select
              key={filter.id}
              value={filter.value}
              onChange={(event) => filter.onChange(event.target.value)}
              className={filter.width ?? 'w-44'}
              aria-label={filter.label}
            >
              {filter.options.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </Select>
          ))}
        </Toolbar>
      </PageHeader>

      {summary}

      <Card bodyClassName="p-0">
        <DataTable
          columns={columns}
          rows={list.items}
          loading={list.isLoading}
          rowKey={rowKey}
          onRowClick={onRowClick}
          empty={
            <EmptyState
              icon={emptyIcon}
              title={emptyTitle}
              description={emptyDescription}
              action={emptyAction}
            />
          }
        />
        <Pagination
          total={list.total}
          limit={list.limit}
          offset={list.offset}
          onChange={list.setOffset}
        />
      </Card>

      {children}
    </div>
  )
}

export const statusOptions = (statuses: string[]) => [
  { value: '', label: 'All statuses' },
  ...statuses.map((status) => ({ value: status, label: status.replace(/_/g, ' ') })),
]
