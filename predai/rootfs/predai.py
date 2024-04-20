from typing import Any
import pandas as pd
import numpy as np
import sqlite3
from datetime import datetime, timedelta, timezone
from neuralprophet import NeuralProphet, set_log_level
import os
import aiohttp
import requests
import asyncio
import json
import ssl
import math
import yaml

TIMEOUT = 240
TIME_FORMAT_HA = "%Y-%m-%dT%H:%M:%S%z"
TIME_FORMAT_HA_DOT = "%Y-%m-%dT%H:%M:%S.%f%z"

def timestr_to_datetime(timestamp):
    """
    Convert a Home Assistant timestamp string to a datetime object.
    """
    try:
        start_time = datetime.strptime(timestamp, TIME_FORMAT_HA)
    except ValueError:
        try:
            start_time = datetime.strptime(timestamp, TIME_FORMAT_HA_DOT)
        except ValueError:
            start_time = None
    if start_time:
        start_time = start_time.replace(second=0, microsecond=0)
    return start_time


class HAInterface():
    def __init__(self):
        self.ha_key = os.environ.get("SUPERVISOR_TOKEN")
        self.ha_url = "http://supervisor/core"
        print("HA Interface started key {} url {}".format(self.ha_key, self.ha_url))

    async def get_history(self, sensor, now, days=7):
        """
        Get the history for a sensor from Home Assistant.

        :param sensor: The sensor to get the history for.
        :return: The history for the sensor.
        """
        start = now - timedelta(days=days)
        end = now
        print("Getting history for sensor {}".format(sensor))
        res = await self.api_call("/api/history/period/{}".format(start.strftime(TIME_FORMAT_HA)), {"filter_entity_id": sensor, "end_time": end.strftime(TIME_FORMAT_HA)})
        if res:
            res = res[0]
            start = timestr_to_datetime(res[0]["last_updated"])
        print("History for sensor {} starts at {}".format(sensor, start))
        return res, start, end

    async def api_call(self, endpoint, datain=None, post=False):
        """
        Make an API call to Home Assistant.

        :param endpoint: The API endpoint to call.
        :param datain: The data to send in the body of the request.
        :param post: True if this is a POST request, False for GET.
        :return: The response from the API.
        """
        url = self.ha_url + endpoint
        print("Making API call to {}".format(url))
        headers = {
            "Authorization": "Bearer " + self.ha_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if post:
            if datain:
                response = await asyncio.to_thread(requests.post, url, headers=headers, json=datain, timeout=TIMEOUT)
            else:
                response = await asyncio.to_thread(requests.post, url, headers=headers, timeout=TIMEOUT)
        else:
            if datain:
                response = await asyncio.to_thread(requests.get, url, headers=headers, params=datain, timeout=TIMEOUT)
            else:
                response = await asyncio.to_thread(requests.get, url, headers=headers, timeout=TIMEOUT)
        print("Response {}".format(response))
        try:
            data = response.json()
        except requests.exceptions.JSONDecodeError:
            print("Failed to decode response from {}".format(url))
            data = None
        except (requests.Timeout, requests.exceptions.ReadTimeout):
            print("Timeout from {}".format(url))
            data = None
        return data

class Prophet:
    def __init__(self, period=30):
        set_log_level("ERROR")
        self.period = period

    async def process_dataset(self, new_data, start_time, end_time, incrementing=False):
        """
        Store the data in the dataset for training.
        """
        dataset = pd.DataFrame(columns=["ds", "y"])
        
        timenow = start_time
        timenow = timenow.replace(second=0, microsecond=0)
        data_index = 0
        value = 0
        last_value = 0
        if incrementing:
            last_value = float(new_data[0]["state"])
        data_len = len(new_data)

        while timenow < end_time and data_index < data_len:
            try:
                value = float(new_data[data_index]["state"])
            except ValueError:
                value = last_value

            last_updated = new_data[data_index]["last_updated"]
            start_time = timestr_to_datetime(last_updated)
        
            if not start_time or start_time < timenow:
                data_index += 1
                continue

            real_value = value
            if incrementing:
                real_value = max(value - last_value, 0)
            dataset.loc[len(dataset)] = {"ds": timenow, "y": real_value}
            last_value = value
            timenow = timenow + timedelta(minutes=self.period)

        print(dataset)
        return dataset, value
    
    async def train(self, dataset, future_periods):
        """
        Train the model on the dataset.
        """
        self.model = NeuralProphet()
        # Fit the model on the dataset (this might take a bit)
        self.metrics = self.model.fit(dataset, freq=(str(self.period) + "min"), progress=None)
        # Create a new dataframe reaching 96 into the future for our forecast, n_historic_predictions also shows historic data
        self.df_future = self.model.make_future_dataframe(dataset, n_historic_predictions=True, periods=future_periods)
        self.forecast = self.model.predict(self.df_future)
        print(self.forecast)
 
    async def save_prediction(self, entity, now, interface, start, incrementing=False, reset_daily=False, units=""):
        """
        Save the prediction to Home Assistant.
        """
        pred = self.forecast
        total = 0
        total_org = 0
        timeseries = {}
        timeseries_org = {}

        for index, row in pred.iterrows():
            ptimestamp = row["ds"].tz_localize(timezone.utc)
            diff = ptimestamp - now
            timestamp = now + diff
            #if timestamp < start:
            #    continue
            time = timestamp.strftime(TIME_FORMAT_HA)
            value = row["yhat1"]
            value_org = row["y"]

            # Daily reset?
            if timestamp <= now:
                if reset_daily and timestamp.hour == 0 and timestamp.minute == 0:
                    total = 0
                    total_org = 0

            total += value
            if not math.isnan(value_org):
                total_org += value_org
            else:
                value_org = None
        
            if incrementing:
                timeseries[time] = round(total, 2)
                if value_org:
                    timeseries_org[time] = round(total_org, 2)
            else:
                timeseries[time] = round(value, 2)
                if value_org:
                    timeseries_org[time] = round(value_org, 2)

        final = total if incrementing else value
        data = {"state": round(final, 2), "attributes": {"last_updated": str(now), "unit_of_measurement": units, "state_class" : "measurement", "results" : timeseries, "source" : timeseries_org}}
        print("Saving prediction to {} last_update {}".format(entity, str(now)))
        await interface.api_call("/api/states/{}".format(entity), data, post=True)

async def subtract_set(dataset, subset, now, incrementing=False):
    """
    Subtract the subset from the dataset.
    """
    pruned = pd.DataFrame(columns=["ds", "y"])
    count = 0
    for index, row in dataset.iterrows():
        ds = row["ds"]
        value = row["y"]
        car_value = 0

        car_row = subset.loc[subset["ds"] == ds]
        if not car_row.empty:
            car_value = car_row["y"].values[0]
            count += 1

        if incrementing:
            value = max(value - car_value, 0)
        else:
            value = value - car_value
        pruned.loc[len(pruned)] = {"ds": ds, "y": value}
    print("Subtracted {} values into new set: {}".format(count, pruned))
    return pruned

class Database():
    def __init__(self):
        self.con = sqlite3.connect('/config/predai.db')
        self.cur = self.con.cursor()

    async def create_table(self, table):
        """
        Create a table in the database by table if it does not exist.
        """
        print("Create table {}".format(table))
        self.cur.execute("CREATE TABLE IF NOT EXISTS {} (timestamp TEXT PRIMARY KEY, value REAL)".format(table))
        self.con.commit()

    async def get_history(self, table):
        """
        Get the history from the database, sorted by timestamp.
        Returns a Dataframe with the history data.
        """
        self.cur.execute("SELECT * FROM {} ORDER BY timestamp".format(table))
        rows = self.cur.fetchall()
        history = pd.DataFrame(columns=["ds", "y"])
        if not rows:
            return history
        for row in rows:
            timestamp = row[0]
            value = row[1]
            history.loc[len(history)] = {"ds": timestamp, "y": value}
        return history

    async def store_history(self, table, history, prev=None):
        """
        Store the history in the database.
        Only the data associated with TIMESTAMPs not already in the database will be stored.
        Returns the updated history DataFrame.

        :param table: The table to store the history in.
        :param history: The history data as a DataFrame.
        """
        added_rows = 0
        prev_values = prev["ds"].values
        prev_values = prev_values.tolist()

        for index, row in history.iterrows():
            timestamp = str(row["ds"])
            value = row["y"]
            if timestamp not in prev_values:
                prev.loc[len(prev)] = {"ds": timestamp, "y": value}
                self.cur.execute("INSERT INTO {} (timestamp, value) VALUES ('{}', {})".format(table, timestamp, value))
                added_rows += 1
        self.con.commit()
        print("Added {} rows to database table {}".format(added_rows, table))
        return prev

async def get_history(interface, nw, sensor_name, now, incrementing, days, use_db):
    """
    Get history from HA, combine it with the database if use_db is True.
    """
    dataset, start, end = await interface.get_history(sensor_name, now, days=days)
    dataset, last_dataset_value = await nw.process_dataset(dataset, start, end, incrementing=incrementing)
    if use_db:
        table_name = sensor_name.replace(".", "_")  # SQLite does not like dots in table names
        db = Database()
        await db.create_table(table_name)
        prev = await db.get_history(table_name)
        dataset = await db.store_history(table_name, dataset, prev)
        print("Stored dataset in database and retrieved full history from database length {}".format(len(dataset)))
    return dataset, start, end
    
async def main():
    """
    Main function for the prediction AI.
    """
    interface = HAInterface()
    while True:
        config = yaml.safe_load(open("/config/predai.yaml"))
        if not config:
            print("WARN: predai.yaml is missing, no work to do")
        else:
            print("Configuration loaded")
            update_every = config.get('update_every', 30)
            sensors = config.get("sensors", [])
            for sensor in sensors:
                sensor_name = sensor.get("name", None)
                subtract_name = sensor.get("subtract", None)
                days = sensor.get("days", 7)
                incrementing = sensor.get("incrementing", False)
                reset_daily = sensor.get("reset_daily", False)
                interval = sensor.get("interval", 30)
                units = sensor.get("units", "")
                future_periods = sensor.get("future_periods", 96)
                use_db = sensor.get("database", False)

                if not sensor_name:
                    continue

                print("Processing sensor {} incrementing {} reset_daily {} interval {} days {} subtract {}".format(sensor_name, incrementing, reset_daily, interval, days, subtract_name))
                
                nw = Prophet(interval)
                now = datetime.now(timezone.utc).astimezone()
                now=now.replace(second=0, microsecond=0, minute=0)

                # Get the data
                dataset, start, end = await get_history(interface, nw, sensor_name, now, incrementing, days, use_db)

                # Get the subtract data
                if subtract_name:
                    subtract_data, start, end = await get_history(interface, nw, subtract_name, now, incrementing, days, use_db)
                else:
                    subtract_data = None

                # Subtract the data
                if subtract_data is not None:
                    print("Subtracting data")
                    pruned = await subtract_set(dataset, subtract_data, now, incrementing=incrementing)
                else:
                    pruned = dataset

                # Start training
                await nw.train(pruned, future_periods)

                # Save the prediction
                await nw.save_prediction(sensor_name + "_prediction", now, interface, start=end, incrementing=incrementing, reset_daily=reset_daily, units=units)

        print("Waiting for {} minutes".format(update_every))
        await asyncio.sleep(60 * update_every)

asyncio.run(main())
