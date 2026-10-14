import { randomBytes, scryptSync } from 'node:crypto';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import pg from 'pg';

const email = process.argv[2]?.trim().toLowerCase();
const pool = new pg.Pool(process.env.DATABASE_URL
  ? { connectionString: process.env.DATABASE_URL }
  : {
      database: process.env.PGDATABASE || 'finances',
      host: process.env.PGHOST || path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../.pgsocket'),
      port: Number(process.env.PGPORT || 55432)
    });

let client;
try {
  client = await pool.connect();
  await client.query('BEGIN');
  const result = email
    ? await client.query('SELECT id,email FROM users WHERE email=$1 FOR UPDATE', [email])
    : await client.query("SELECT id,email FROM users WHERE role='admin' FOR UPDATE");
  if (result.rowCount !== 1) throw new Error(email ? 'Email não encontrado.' : 'Administrador não encontrado ou ambíguo. Informe o email.');

  const user = result.rows[0];
  const password = randomBytes(18).toString('base64url');
  const salt = randomBytes(16).toString('hex');
  const hash = `${salt}:${scryptSync(password, salt, 64).toString('hex')}`;
  await client.query('UPDATE users SET password_hash=$1 WHERE id=$2', [hash, user.id]);
  await client.query('DELETE FROM sessions WHERE user_id=$1', [user.id]);
  await client.query('COMMIT');
  console.log(`Email: ${user.email}`);
  console.log(`Senha temporária: ${password}`);
  console.log('Entre no site e troque a senha em Perfil.');
} catch (error) {
  if (client) await client.query('ROLLBACK');
  console.error(`Não foi possível redefinir a senha: ${error.message}`);
  process.exitCode = 1;
} finally {
  client?.release();
  await pool.end();
}
