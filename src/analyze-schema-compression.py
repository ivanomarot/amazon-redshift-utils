#!/usr/bin/env python

'''
analyze-schema-compression.py
v .9.1.2

* Copyright 2014, Amazon.com, Inc. or its affiliates. All Rights Reserved.
*
* Licensed under the Amazon Software License (the "License").
* You may not use this file except in compliance with the License.
* A copy of the License is located at
*
* http://aws.amazon.com/asl/
*
* or in the "license" file accompanying this file. This file is distributed
* on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either
* express or implied. See the License for the specific language governing
* permissions and limitations under the License.

Analyses all tables in a Redshift Cluster Schema, and outputs a SQL script to 
migrate database tables with sub-optimal column encodings to optimal column
encodings as recommended by the database engine.

The processing model that the script will generate is:
    create new table XXX_$mig
    insert select * from old table into new table
    analyze new table
    rename old table to XXX_$old or drop table
    rename new table to old table

Use with caution on a running system


Ian Meyers
Amazon Web Services (2014)
'''

import sys
from multiprocessing import Pool
import psycopg2
import getopt
import os
import re
import getpass
import time

VERSION = ".9.1.1"

OK = 0
ERROR = 1
INVALID_ARGS = 2
NO_WORK = 3
TERMINATED_BY_USER = 4
NO_CONNECTION = 5

# timeout for retries - 100ms
RETRY_TIMEOUT = 100/1000
    
master_conn = None
db_connections = {}
db = None
db_user = None
db_pwd = None
db_host = None
db_port = 5439
analyze_schema = 'public'
target_schema = None
analyze_table = None
debug = False
threads = 2
output_file_handle = None
do_execute = False
query_slot_count = 1
ignore_errors = False
force = False
drop_old_data = False
comprows = None
    
def cleanup():
    # close all connections and close the output file
    if master_conn != None:
        master_conn.close()
    
    for key in db_connections:
        if db_connections[key] != None:
            db_connections[key].close() 
    
    if output_file_handle != None:
        output_file_handle.close()

def comment(string):
    if re.match('.*\\n.*',string) != None:
        write('/* [%s]\n%s\n*/\n' % (str(os.getpid()),string))
    else:
        write('-- [%s] %s' % (str(os.getpid()),string))

def print_statements(statements):
    if statements != None:
        for s in statements:
            if s != None:
                write(s)
        
def write(s):
    # write output to all the places we want it
    print(s)
    if output_file_handle != None:
        output_file_handle.write(s)
        output_file_handle.flush()
    
def get_conn():
    global db_connections
    pid = str(os.getpid())
    
    conn = None
    
    # get the database connection for this PID
    try:
        conn = db_connections[pid]
    except KeyError:
        pass
        
    if conn == None:
        # connect to the database
        if debug:
            comment('Connect [%s] %s:%s:%s:%s' % (pid,db_host,db_port,db,db_user))
            
        try:
            conn = psycopg2.connect(database=db, user=db_user, host=db_host, password=db_pwd, port=db_port)
        except:
            write('Unable to connect to Cluster Endpoint')
            
            return None
        
        # turn off the default autocommit behaviour
        conn.autocommit = False
        
        # set default search path
        cur = conn.cursor()
        search_path = 'set search_path = \'$user\',public,%s' % (analyze_schema,)
        if target_schema != None:
            search_path = search_path + ', %s' % (target_schema)
            
        if debug:
            comment(search_path)
        
        try:
            cur.execute(search_path)
        except psycopg2.ProgrammingError as e:
            if re.match('schema "%s" does not exist' % (analyze_schema,),e.message) != None:
                write('Schema %s does not exist' % (analyze_schema,))
            else:
                write(e.message)
            return None
        
        if query_slot_count != 1:
            set_slot_count = 'set wlm_query_slot_count = %s' % (query_slot_count)
            
            if debug:
                comment(set_slot_count)
                
            cur.execute(set_slot_count)
            
        # set a long statement timeout
        set_timeout = "set statement_timeout = '1200000'"
        if debug:
            comment(set_timeout)
            
        cur.execute(set_timeout)
        
        # cache the connection
        db_connections[pid] = conn
        
    return conn

def get_table_attribute(description_list,column_name,index):
    # get a specific value requested from the table description structure based on the index and column name
    # runs against output from get_table_desc()
    for item in description_list:
        if item[0] == column_name:
            return item[index]
        
def get_table_desc(conn,table_name):
    # get the table definition from the dictionary so that we can get relevant details for each column
    desc_cur = conn.cursor();
    statement = '''select "column", type, encoding, distkey, sortkey, "notnull"
 from pg_table_def
 where schemaname = '%s'
 and tablename = '%s'
''' % (analyze_schema,table_name)

    if debug:
        comment(statement)
        
    desc_cur.execute(statement);
    description = desc_cur.fetchall()
    desc_cur.close()
    
    return description

def run_commands(conn,commands):
    # RUNIT
    cur = conn.cursor()

    for c in commands:
        comment('[%s] Running %s: \n' % (str(os.getpid()),c))
            
        try:
            cur.execute(c)            
            comment(cur.statusmessage)
        except Exception as e:
            # cowardly bail on errors
            conn.rollback()
            write(e.message)
            return False
        
    cur.close()
    
    return True
        
def analyze(tables):
    # get a connection from the connection pool
    local_conn = get_conn()
    analyze_cur = local_conn.cursor();
        
    table_name = tables[0]
        
    statement = 'analyze compression %s' % (table_name,)
    
    if comprows != None:
        statement = statement + (" comprows %s" % (comprows,))
        
    try:
        if debug:
            comment(statement)
            
        write("-- Analysing Table '%s'\n" % (table_name,))
    
        output = None
        analyze_retry = 10
        attempt_count = 0
        last_exception = None
        while attempt_count < analyze_retry and output == None:
            try:
                analyze_cur.execute(statement)
                output = analyze_cur.fetchall()
                analyze_cur.close()
            except KeyboardInterrupt:
                # To handle Ctrl-C from user
                analyze_cur.close()
                cleanup()
                sys.exit(TERMINATED_BY_USER)
            except Exception as e:
                attempt_count += 1
                last_exception = e
                local_conn.rollback
                
                # Exponential Backoff
                time.sleep(2**attempt_count * RETRY_TIMEOUT)

        if output == None:
            print "Unable to Analyze %s due to Exception %s" % (table_name,last_exception.message)
            return ERROR
        
        if target_schema == analyze_schema:
            target_table = '%s_$mig' % (table_name,)
        else:
            target_table = table_name
        
        create_table = '-- creating migration table for %s\nbegin;\n\ncreate table %s.%s(' % (table_name,target_schema,target_table,)
        
        # query the table column definition
        descr = get_table_desc(local_conn,table_name)
        found_non_raw = False
        encode_columns = []
        statements = []
        sortkeys = {}
        has_zindex_sortkeys = False
        
        # process each item given back by the analyze request
        for row in output:
            col = row[1]
            
            # only proceed with generating an output script if we found any non-raw column encodings
            if row[2] != 'raw':
                found_non_raw = True
            
            col_type = get_table_attribute(descr,col,1)
            # fix datatypes
            col_type = col_type.replace('character varying','varchar').replace('without time zone','')
            
            # is this the dist key?
            distkey = get_table_attribute(descr,col,3)
            if distkey or str(distkey).upper()=='TRUE':
                distkey='DISTKEY'
            else:
                distkey = ''
                
            # is this the sort key?
            sortkey = get_table_attribute(descr,col,4)
            if sortkey != 0:
                # add the absolute ordering of the sortkey to the list of all sortkeys
                sortkeys[abs(sortkey)] = col
                
                if (sortkey < 0):
                    has_zindex_sortkeys = True
                
            # don't compress first sort key
            if abs(sortkey) == 1:
                compression = 'RAW'
            else:
                compression = row[2]
                
            # extract null/not null setting
            col_null = get_table_attribute(descr,col,5)
            if col_null == 'true':
                col_null = 'NOT NULL'
            else:
                col_null = ''
                
            # add the formatted column specification
            encode_columns.extend(['%s %s %s encode %s %s' % (col, col_type, col_null, compression, distkey)])            
                        
        if found_non_raw or force:
            # add all the column encoding statements on to the create table statement, suppressing the leading comma on the first one
            for i, s in enumerate(encode_columns):
                create_table += '\n%s%s' % ('' if i == 0 else ',',s)
    
            create_table = create_table + '\n)'
            
            # add sort key as a table block to accommodate multiple columns
            if len(sortkeys) > 0:
                sortkey = '\n%sSORTKEY(' % ('INTERLEAVED ' if has_zindex_sortkeys else '')    
                
                for i in range(1, len(sortkeys)+1):
                    sortkey = sortkey + sortkeys[i]
                   
                    if i != len(sortkeys):
                       sortkey = sortkey + ','
                    else:
                       sortkey = sortkey + ')\n'
                create_table = create_table + (' %s ' % sortkey)                
            
            create_table = create_table + ';\n'
            
            # run the create table statement
            statements.extend([create_table])         
                    
            # insert the old data into the new table
            insert = '-- migrating data to new structure\ninsert into %s.%s select * from %s.%s;\n' % (target_schema,target_table,analyze_schema,tables[0])
            statements.extend([insert])
                    
            # analyze the new table
            analyze = 'analyze %s.%s;\n' % (target_schema,target_table)
            statements.extend([analyze])
                    
            if (target_schema == analyze_schema):
                # rename the old table to _$old or drop
                if drop_old_data:
                    drop = 'drop table %s.%s;\n' % (target_schema,table_name)
                else:
                    drop = 'alter table %s.%s rename to %s;\n' % (target_schema,table_name,table_name + "_$old")                
                
                statements.extend([drop])
                        
                # rename the migrate table to the old table name
                rename = 'alter table %s.%s rename to %s;\n' % (target_schema,target_table,table_name)
                statements.extend([rename])        
            
            statements.extend(['commit;\n'])
            
            if do_execute:
                if not run_commands(local_conn,statements):
                    if not ignore_errors:
                        return ERROR     
            
    except Exception as e:
        write('Exception %s during analysis of %s' % (e.message,table_name))
        write(e)
        return ERROR
        
    analyze_cur.close()
    
    print_statements(statements)
    
    return OK

def usage():
    write('Usage: analyze-schema-compression.py')
    write('       Generates a script to optimise Redshift column encodings on all tables in a schema')
    write('')
    write('Arguments: --db             - The Database to Use')
    write('           --db-user        - The Database User to connect to')
    write('           --db-host        - The Cluster endpoint')
    write('           --db-port        - The Cluster endpoint port (default 5439)')
    write('           --analyze-schema - The Schema to be Analyzed (default public)')
    write('           --analyze-table  - A specific table to be Analyzed, if --analyze-schema is not desired')
    write('           --target-schema  - Name of a Schema into which the newly optimised tables and data should be created, rather than in place')
    write('           --threads        - The number of concurrent connections to use during analysis (default 2)')
    write('           --output-file    - The full path to the output file to be generated')
    write('           --debug          - Generate Debug Output including SQL Statements being run')
    write('           --do-execute     - Run the compression encoding optimisation')
    write('           --slot-count     - Modify the wlm_query_slot_count from the default of 1')
    write('           --ignore-errors  - Ignore errors raised in threads when running and continue processing')
    write('           --force          - Force table migration even if the table already has Column Encoding applied')
    write('           --drop-old-data  - Drop the old version of the data table, rather than renaming')
    write('           --comprows       - Set the number of rows to use for Compression Encoding Analysis')
    sys.exit(INVALID_ARGS)


def main(argv):
    supported_args = """db= db-user= db-host= db-port= target-schema= analyze-schema= analyze-table= threads= debug= output-file= do-execute= slot-count= ignore-errors= force= drop-old-data= comprows="""
    
    # extract the command line arguments
    try:
        optlist, remaining = getopt.getopt(sys.argv[1:], "", supported_args.split())
    except getopt.GetoptError as err:
        print str(err)
        usage()
    
    # setup globals
    global master_conn
    global db
    global db_user
    global db_pwd
    global db_host
    global db_port
    global threads
    global analyze_schema
    global analyze_table
    global target_schema
    global debug
    global output_file_handle
    global do_execute
    global query_slot_count
    global ignore_errors
    global force
    global drop_old_data
    global comprows
    
    output_file = None

    # parse command line arguments
    for arg, value in optlist:
        if arg == "--db":
            if value == '' or value == None:
                usage()
            else:
                db = value
        elif arg == "--db-user":
            if value == '' or value == None:
                usage()
            else:
                db_user = value
        elif arg == "--db-host":
            if value == '' or value == None:
                usage()
            else:
                db_host = value
        elif arg == "--db-port":
            if value != '' and value != None:
                db_port = value
        elif arg == "--analyze-schema":
            if value != '' and value != None:
                analyze_schema = value
        elif arg == "--analyze-table":
            if value != '' and value != None:
                analyze_table = value
        elif arg == "--target-schema":
            if value != '' and value != None:
                target_schema = value
        elif arg == "--threads":
            if value != '' and value != None:
                threads = int(value)
        elif arg == "--debug":
            if value == 'true' or value == 'True':
                debug = True
            else:
                debug = False
        elif arg == "--output-file":
            if value == '' or value == None:
                usage()
            else:
                output_file = value
        elif arg == "--ignore-errors":
            if value == 'true' or value == 'True':
                ignore_errors = True
            else:
                ignore_errors = False
        elif arg == "--force":
            if value == 'true' or value == 'True':
                force = True
            else:
                force = False
        elif arg == "--drop-old-data":
            if value == 'true' or value == 'True':
                drop_old_data = True
            else:
                drop_old_data = False
        elif arg == "--do-execute":
            if value == 'true' or value == 'True':
                do_execute = True
            else:
                do_execute = False
        elif arg == "--slot-count":
            query_slot_count = int(value)
        elif arg == "--comprows":
            comprows = int(value)
        else:
            assert False, "Unsupported Argument " + arg
            usage()
    
    # Validate that we've got all the args needed
    if db == None or db_user == None or db_host == None or \
    db_port == None or output_file == None:
        usage()
    
    if target_schema == None:
        target_schema = analyze_schema
        
    # Reduce to 1 thread if we're analyzing a single table
    if analyze_table != None:
        threads = 1
        
    # get the database password
    db_pwd = getpass.getpass("Password <%s>: " % db_user)
    
    # open the output file
    output_file_handle = open(output_file,'w')
    
    # get a connection for the controlling processes
    master_conn = get_conn()
    
    if master_conn == None:
        sys.exit(NO_CONNECTION)
    
    write("-- Connected to %s:%s:%s as %s\n" % (db_host, db_port, db, db_user))
    if analyze_table != None:
        snippet = "Table '%s'" % analyze_table        
    else:
        snippet = "Schema '%s'" % analyze_schema
        
    write("-- Analyzing %s for Columnar Encoding Optimisations with %s Threads...\n" % (snippet,threads))
    
    if do_execute:
        if drop_old_data:
            really_go = getpass.getpass("This will make irreversible changes to your database, and cannot be undone. Type 'Yes' to continue: ")
            
            if not really_go == 'Yes':
                write("Terminating on User Request")
                sys.exit(TERMINATED_BY_USER)

        write("-- Recommended encoding changes will be applied automatically...\n")
    else:
        write("\n")
    
    cur = master_conn.cursor()    
    
    if analyze_table != None:        
        statement = '''select trim(a.name) as table, b.mbytes, a.rows
from (select db_id, id, name, sum(rows) as rows from stv_tbl_perm a group by db_id, id, name) as a
join pg_class as pgc on pgc.oid = a.id
join (select tbl, count(*) as mbytes
from stv_blocklist group by tbl) b on a.id=b.tbl
and pgc.relname = '%s'        
        ''' % (analyze_table,)        
    else:
        # query for all tables in the schema ordered by size descending
        write("-- Extracting Candidate Table List...\n")
        
        statement = '''select trim(a.name) as table, b.mbytes, a.rows
from (select db_id, id, name, sum(rows) as rows from stv_tbl_perm a group by db_id, id, name) as a
join pg_class as pgc on pgc.oid = a.id
join pg_namespace as pgn on pgn.oid = pgc.relnamespace
join (select tbl, count(*) as mbytes
from stv_blocklist group by tbl) b on a.id=b.tbl
where pgn.nspname = '%s'
  and trim(a.name) not like '%%_$old'
  and trim(a.name) not like '%%_$mig'
order by 2
        ''' % (analyze_schema,)
    
    if debug:
        comment(statement)
        
    cur.execute(statement)
    analyze_tables = cur.fetchall()
    cur.close()
    
    write("-- Analyzing %s table(s)" % (len(analyze_tables)))

    # setup executor pool
    p = Pool(threads)
    worker_output = []
    
    if analyze_tables != None:
        try:
            # run all concurrent steps and block on completion
            result = p.map(analyze,analyze_tables)
            worker_output.append([result])            
        except KeyboardInterrupt:
            # To handle Ctrl-C from user
            p.close()
            p.terminate()
            cleanup()
            sys.exit(TERMINATED_BY_USER)
        except:
            p.close()
            p.terminate()
            cleanup()
            sys.exit(ERROR)
    else:
        comment("No Tables Found to Analyze")
        
    # do a final vacuum if needed
    if drop_old_data:
        write("vacuum delete only;\n")

    p.terminate()
    comment('Processing Complete')
    cleanup()
    
    # return any non-zero worker output statuses
    for ret in worker_output:
        if ret != OK:
            sys.exit(ret)
            
    sys.exit(OK)

if __name__ == "__main__":
    main(sys.argv)