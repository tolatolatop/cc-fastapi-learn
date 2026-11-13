import assert from 'node:assert/strict'
import test from 'node:test'

import { normalizeTaskId, readTaskId, taskUrlFor } from './taskUrl.ts'

const TASK_ID = '5f2c1c7e-8f2b-4c4f-9a4e-1b3d2c6a7e11'

test('normalizeTaskId trims values and drops the TASK- prefix', () => {
  assert.equal(normalizeTaskId(`  ${TASK_ID}  `), TASK_ID)
  assert.equal(normalizeTaskId('TASK-5f2c1c7e'), '5f2c1c7e')
  assert.equal(normalizeTaskId('task:5f2c1c7e'), '5f2c1c7e')
  assert.equal(normalizeTaskId(''), null)
  assert.equal(normalizeTaskId('   '), null)
  assert.equal(normalizeTaskId(null), null)
  assert.equal(normalizeTaskId(undefined), null)
})

test('readTaskId reads ?task= from the query string', () => {
  assert.equal(readTaskId({ search: `?task=${TASK_ID}`, hash: '' }), TASK_ID)
  assert.equal(readTaskId({ search: '?task=TASK-5f2c1c7e', hash: '#/tasks' }), '5f2c1c7e')
  assert.equal(readTaskId({ search: '?task=', hash: '#/tasks' }), null)
  assert.equal(readTaskId({ search: '', hash: '' }), null)
  assert.equal(readTaskId({ search: '?queue=default', hash: '#/tasks' }), null)
})

test('readTaskId accepts task_id aliases', () => {
  assert.equal(readTaskId({ search: `?task_id=${TASK_ID}`, hash: '' }), TASK_ID)
  assert.equal(readTaskId({ search: `?taskId=${TASK_ID}`, hash: '' }), TASK_ID)
})

test('readTaskId falls back to a hash-embedded parameter', () => {
  assert.equal(readTaskId({ search: '', hash: `#/tasks?task=${TASK_ID}` }), TASK_ID)
})

test('readTaskId prefers the query string over the hash', () => {
  assert.equal(
    readTaskId({ search: '?task=from-query', hash: '#/tasks?task=from-hash' }),
    'from-query',
  )
})

test('taskUrlFor writes the task id and keeps the current view hash', () => {
  assert.equal(
    taskUrlFor(TASK_ID, `http://console.local/?foo=bar#/tasks`),
    `/?foo=bar&task=${TASK_ID}#/tasks`,
  )
  assert.equal(
    taskUrlFor(`TASK-${TASK_ID}`, 'http://console.local/#/tasks'),
    `/?task=${TASK_ID}#/tasks`,
  )
})

test('taskUrlFor removes the parameter when the drawer closes', () => {
  assert.equal(
    taskUrlFor(null, `http://console.local/?task=${TASK_ID}&foo=bar#/tasks`),
    '/?foo=bar#/tasks',
  )
  assert.equal(taskUrlFor(null, 'http://console.local/#/reviews'), '/#/reviews')
})

test('taskUrlFor normalises alias and hash parameters to a single ?task=', () => {
  assert.equal(
    taskUrlFor(TASK_ID, 'http://console.local/?task_id=stale#/tasks?task=stale&view=board'),
    `/?task=${TASK_ID}#/tasks?view=board`,
  )
})
